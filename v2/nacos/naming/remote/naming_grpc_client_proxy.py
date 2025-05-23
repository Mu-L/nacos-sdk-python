import asyncio
import base64
import hashlib
import hmac
import logging
import uuid
from typing import Optional, List

from v2.nacos.common.client_config import ClientConfig
from v2.nacos.common.constants import Constants
from v2.nacos.common.nacos_exception import NacosException, SERVER_ERROR
from v2.nacos.naming.cache.service_info_cache import ServiceInfoCache
from v2.nacos.naming.model.instance import Instance
from v2.nacos.naming.model.naming_param import ListServiceParam
from v2.nacos.naming.model.naming_request import InstanceRequest, NOTIFY_SUBSCRIBER_REQUEST_TYPE, \
    SubscribeServiceRequest, AbstractNamingRequest, ServiceListRequest, BatchInstanceRequest
from v2.nacos.naming.model.naming_response import SubscribeServiceResponse, InstanceResponse, ServiceListResponse, \
    BatchInstanceResponse
from v2.nacos.naming.model.service import Service
from v2.nacos.naming.model.service import ServiceList
from v2.nacos.naming.remote.naming_grpc_connection_event_listener import NamingGrpcConnectionEventListener
from v2.nacos.naming.remote.naming_push_request_handler import NamingPushRequestHandler
from v2.nacos.naming.util.naming_client_util import get_group_name
from v2.nacos.naming.util.naming_remote_constants import NamingRemoteConstants
from v2.nacos.transport.http_agent import HttpAgent
from v2.nacos.transport.nacos_server_connector import NacosServerConnector
from v2.nacos.transport.rpc_client import ConnectionType
from v2.nacos.transport.rpc_client_factory import RpcClientFactory
from v2.nacos.utils.common_util import get_current_time_millis, to_json_string


class NamingGRPCClientProxy:
    DEFAULT_SERVER_PORT = 8848

    def __init__(self,
                 client_config: ClientConfig,
                 http_client: HttpAgent,
                 service_info_cache: ServiceInfoCache):
        self.logger = logging.getLogger(Constants.NAMING_MODULE)
        self.client_config = client_config
        self.uuid = uuid.uuid4()

        self.service_info_cache = service_info_cache
        self.rpc_client = None
        self.namespace_id = client_config.namespace_id
        self.nacos_server_connector = NacosServerConnector(self.logger, client_config, http_client)
        self.event_listener = NamingGrpcConnectionEventListener(self)

    async def start(self):
        await self.nacos_server_connector.init()
        labels = {Constants.LABEL_SOURCE: Constants.LABEL_SOURCE_SDK,
                  Constants.LABEL_MODULE: Constants.NAMING_MODULE}
        self.rpc_client = await RpcClientFactory(self.logger).create_client(str(self.uuid), ConnectionType.GRPC, labels,
                                                                            self.client_config,
                                                                            self.nacos_server_connector)
        await self.rpc_client.register_server_request_handler(NOTIFY_SUBSCRIBER_REQUEST_TYPE,
                                                              NamingPushRequestHandler(self.logger,
                                                                                       self.service_info_cache))
        await self.rpc_client.register_connection_listener(self.event_listener)

        await self.rpc_client.start()

    async def request_naming_server(self, request: AbstractNamingRequest, response_class):
        try:
            await self.nacos_server_connector.inject_security_info(request.get_headers())

            credentials = self.client_config.credentials_provider.get_credentials()
            if credentials.get_access_key_id() and credentials.get_access_key_secret():
                service_name = get_group_name(request.serviceName, request.groupName)
                if service_name.strip():
                    sign_str = str(get_current_time_millis()) + Constants.SERVICE_INFO_SPLITER + service_name
                else:
                    sign_str = str(get_current_time_millis())

                request.put_all_headers({
                    "ak": credentials.get_access_key_id(),
                    "data": sign_str,
                    "signature": base64.encodebytes(hmac.new(credentials.get_access_key_secret().encode(), sign_str.encode(),
                                                             digestmod=hashlib.sha1).digest()).decode().strip()
                })
                if credentials.get_security_token():
                    request.put_header("Spas-SecurityToken", credentials.get_security_token())

            response = await self.rpc_client.request(request, self.client_config.grpc_config.grpc_timeout)
            if response.get_result_code() != 200:
                raise NacosException(response.get_error_code(), response.get_message())
            if issubclass(response.__class__, response_class):  # todo check and fix if anything wrong
                return response
            raise NacosException(SERVER_ERROR, " Server return invalid response")
        except NacosException as e:
            self.logger.error("failed to invoke nacos naming server : " + str(e))
            raise e
        except Exception as e:
            self.logger.error("failed to invoke nacos naming server : " + str(e))
            raise NacosException(SERVER_ERROR, "Request nacos naming server failed: " + str(e))

    async def register_instance(self, service_name: str, group_name: str, instance: Instance):
        self.logger.info("register instance service_name:%s, group_name:%s, namespace:%s, instance:%s" % (
            service_name, group_name, self.namespace_id, str(instance)))
        await self.event_listener.cache_instance_for_redo(service_name, group_name, instance)
        request = InstanceRequest(
            namespace=self.namespace_id,
            serviceName=service_name,
            groupName=group_name,
            instance=instance,
            type=NamingRemoteConstants.REGISTER_INSTANCE)
        response = await self.request_naming_server(request, InstanceResponse)
        return response.is_success()

    async def batch_register_instance(self, service_name: str, group_name: str, instances: List[Instance]) -> bool:
        self.logger.info("batch register instance service_name:%s, group_name:%s, namespace:%s,instances:%s" % (
            service_name, group_name, self.namespace_id, str(instances)))

        await self.event_listener.cache_instances_for_redo(service_name, group_name, instances)
        request = BatchInstanceRequest(
            namespace=self.namespace_id,
            serviceName=service_name,
            groupName=group_name,
            instances=instances,
            type=NamingRemoteConstants.BATCH_REGISTER_INSTANCE)
        response = await self.request_naming_server(request, BatchInstanceResponse)
        return response.is_success()

    async def deregister_instance(self, service_name: str, group_name: str, instance: Instance) -> bool:
        self.logger.info("deregister instance ip:%s, port:%s, service_name:%s, group_name:%s, namespace:%s" % (
            instance.ip, instance.port, service_name, group_name, self.namespace_id))
        request = InstanceRequest(
            namespace=self.namespace_id,
            serviceName=service_name,
            groupName=group_name,
            instance=instance,
            type=NamingRemoteConstants.DE_REGISTER_INSTANCE)
        response = await self.request_naming_server(request, InstanceResponse)
        await self.event_listener.remove_instance_for_redo(service_name, group_name)
        return response.is_success()

    async def list_services(self, param: ListServiceParam) -> ServiceList:
        self.logger.info("listService group_name:%s, namespace:%s", param.group_name, param.namespace_id)
        request = ServiceListRequest(
            namespace=param.namespace_id,
            groupName=param.group_name,
            serviceName='',
            pageNo=param.page_no,
            pageSize=param.page_size)
        response = await self.request_naming_server(request, ServiceListResponse)
        return ServiceList(
            count=response.count,
            services=response.serviceNames
        )

    async def subscribe(self, service_name: str, group_name: str, clusters: str) -> Optional[Service]:
        self.logger.info("subscribe service_name:%s, group_name:%s, clusters:%s, namespace:%s",
                         service_name, group_name, clusters, self.namespace_id)

        await self.event_listener.cache_subscribe_for_redo(get_group_name(service_name, group_name), clusters)

        request = SubscribeServiceRequest(
            namespace=self.namespace_id,
            groupName=group_name,
            serviceName=service_name,
            clusters=clusters,
            subscribe=True)

        request.put_header("app", self.client_config.app_name)
        response = await self.request_naming_server(request, SubscribeServiceResponse)
        if not response.is_success():
            self.logger.error(
                "failed to subscribe service_name:%s, group_name:%s, clusters:%s, namespace:%s, response:%s",
                service_name, group_name, clusters, self.namespace_id, response)
            return None

        return response.serviceInfo

    async def unsubscribe(self, service_name: str, group_name: str, clusters: str):
        self.logger.info("unSubscribe service_name:%s, group_name:%s, clusters:%s, namespace:%s",
                         service_name, group_name, clusters, self.namespace_id)
        await self.event_listener.remove_subscriber_for_redo(get_group_name(service_name, group_name), clusters)

        _ = await self.request_naming_server(SubscribeServiceRequest(
            namespace=self.namespace_id,
            groupName=group_name,
            serviceName=service_name,
            clusters=clusters,
            subscribe=False
        ), SubscribeServiceResponse)
        return

    async def close_client(self):
        self.logger.info("close Nacos python naming grpc client...")
        await self.rpc_client.shutdown()

    def server_health(self):
        return self.rpc_client.is_running()
