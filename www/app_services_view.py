# -*- coding: utf8 -*-
import logging
import json
from decimal import Decimal

from dateutil.relativedelta import relativedelta
from django.views.decorators.cache import never_cache
from django.template.response import TemplateResponse
from django.http.response import HttpResponse
from django.http import JsonResponse

from share.manager.region_provier import RegionProviderManager
from www.models.main import ServiceGroupRelation, ServiceAttachInfo, TenantServiceEnvVar, TenantServiceMountRelation, \
    TenantServiceVolume, ServiceCreateStep, ServiceFeeBill
from www.views import BaseView, AuthedView, LeftSideBarMixin, CopyPortAndEnvMixin
from www.decorator import perm_required
from www.models import ServiceInfo, TenantServicesPort, TenantServiceInfo, TenantServiceRelation, TenantServiceEnv, TenantServiceAuth
from service_http import RegionServiceApi
from www.tenantservice.baseservice import BaseTenantService, TenantUsedResource, TenantAccountService, CodeRepositoriesService, TenantRegionService, \
    AppCreateService
from www.utils.language import is_redirect
from www.monitorservice.monitorhook import MonitorHook
from www.utils.crypt import make_uuid
from django.conf import settings
from www.servicetype import ServiceType
from www.utils import sn
import datetime

logger = logging.getLogger('default')

regionClient = RegionServiceApi()
monitorhook = MonitorHook()
tenantAccountService = TenantAccountService()
tenantUsedResource = TenantUsedResource()
baseService = BaseTenantService()
codeRepositoriesService = CodeRepositoriesService()
tenantRegionService = TenantRegionService()
rpmManager = RegionProviderManager()
appCreateService = AppCreateService()


class AppCreateView(LeftSideBarMixin, AuthedView):

    def get_media(self):
        media = super(AuthedView, self).get_media() + self.vendor(
            'www/css/goodrainstyle.css', 'www/css/style.css', 'www/css/style-responsive.css', 'www/js/jquery.cookie.js',
            'www/js/common-scripts.js', 'www/js/jquery.dcjqaccordion.2.7.js', 'www/js/jquery.scrollTo.min.js',
            'www/js/respond.min.js')
        return media

    @never_cache
    @perm_required('create_service')
    def get(self, request, *args, **kwargs):
        # 检查用户邮箱是否完善,跳转到邮箱完善页面
        next_url = request.path
        if self.user.email is None or self.user.email == "":
            return self.redirect_to("/wechat/info?next={0}".format(next_url))
        # 判断系统中是否有初始化的application数据
        count = ServiceInfo.objects.filter(service_key="application").count()
        if count == 0:
            # 跳转到云市引用市场下在application模版
            redirect_url = "/ajax/{0}/remote/market?service_key=application&app_version=81701&next_url={1}".format(self.tenantName, next_url)
            logger.debug("now init application record")
            return self.redirect_to(redirect_url)

        context = self.get_context()
        response = TemplateResponse(self.request, "www/app_create_step_1.html", context)
        try:
            type = request.GET.get("type", "gitlab_demo")
            if type not in("gitlab_new","gitlab_manual","github","gitlab_exit","gitlab_demo","gitlab_self",):
                type = "gitlab_self"
            context["tenantName"] = self.tenantName
            context["createApp"] = "active"
            request.session["app_tenant"] = self.tenantName
            app_status = request.COOKIES.get('app_status', '')
            app_an = request.COOKIES.get('app_an', '')
            context["app_status"] = app_status
            context["app_an"] = app_an
            context["cur_type"] = type
            context["is_private"] = sn.instance.is_private()
            response.delete_cookie('app_status')
            response.delete_cookie('app_an')

            regionBo = rpmManager.get_work_region_by_name(self.response_region)
            context['pre_paid_memory_price'] = regionBo.memory_package_price
            context['post_paid_memory_price'] = regionBo.memory_trial_price
            context['pre_paid_disk_price'] = regionBo.disk_package_price
            context['post_paid_disk_price'] = regionBo.disk_trial_price
            context['post_paid_net_price'] = regionBo.net_trial_price
            # 是否为免费租户
            context['is_tenant_free'] = (self.tenant.pay_type == "free")

            # context['cloud_assistant'] = sn.instance.cloud_assistant
            context["is_private"] = sn.instance.is_private()
            # 判断云帮是否为公有云
            context["is_public_clound"] = sn.instance.cloud_assistant == "goodrain" and (not sn.instance.is_private())


        except Exception as e:
            logger.exception(e)
        return response

    @never_cache
    @perm_required('create_service')
    def post(self, request, *args, **kwargs):
        service_alias = ""
        service_code_from = ""
        tenant_id = self.tenant.tenant_id
        service_id = make_uuid(tenant_id)
        data = {}
        try:
            # judge region tenant is init
            success = tenantRegionService.init_for_region(self.response_region, self.tenantName, tenant_id, self.user)
            if not success:
                data["status"] = "failure"
                return JsonResponse(data, status=200)

            # if tenantAccountService.isOwnedMoney(self.tenant, self.response_region):
            #     data["status"] = "owed"
            #     return JsonResponse(data, status=200)

            # if tenantAccountService.isExpired(self.tenant,self.service):
            #     data["status"] = "expired"
            #     return JsonResponse(data, status=200)

            service_desc = ""
            service_cname = request.POST.get("create_app_name", "")
            service_code_from = request.POST.get("service_code_from", "")
            if service_code_from is None or service_code_from == "":
                data["status"] = "code_from"
                return JsonResponse(data, status=200)
            service_cname = service_cname.rstrip().lstrip()
            if service_cname is None or service_cname == "":
                data["status"] = "empty"
                return JsonResponse(data, status=200)
            # get base service
            service = ServiceInfo.objects.get(service_key="application")
            # 根据页面参数获取节点数和每个节点的内存大小
            min_memory = int(request.POST.get("service_min_memory", 128))
            # 将G转换为M
            if min_memory < 128:
                min_memory *= 1024
            min_node = int(request.POST.get("service_min_node", 1))
            service.min_memory = min_memory
            service.min_node = min_node

            # calculate resource
            tempService = TenantServiceInfo()
            tempService.min_memory = service.min_memory
            tempService.service_region = self.response_region
            tempService.min_node = service.min_node
            diffMemory = service.min_node * service.min_memory
            rt_type, flag = tenantUsedResource.predict_next_memory(self.tenant, tempService, diffMemory, False)
            if not flag:
                if rt_type == "memory":
                    data["status"] = "over_memory"
                    data["tenant_type"] = self.tenant.pay_type
                else:
                    data["status"] = "over_money"
                return JsonResponse(data, status=200)

            service_alias = "gr"+service_id[-6:]
            # save service attach info
            memory_pay_method = request.POST.get("memory_pay_method", "prepaid")
            disk_pay_method = request.POST.get("disk_pay_method", "prepaid")
            pre_paid_period = int(request.POST.get("pre_paid_period", 1))
            disk = int(request.POST.get("disk_num", 0))
            # 将G转换为M
            if disk < 1024:
                disk *= 1024
            create_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            startTime = datetime.datetime.now() + datetime.timedelta(hours=1)
            endTime = startTime + relativedelta(months=int(pre_paid_period))
            # 保存配套信息
            sai = ServiceAttachInfo()
            sai.tenant_id = tenant_id
            sai.service_id = service_id
            sai.memory_pay_method = memory_pay_method
            sai.disk_pay_method = disk_pay_method
            sai.min_memory = min_memory
            sai.min_node = min_node
            sai.disk = disk
            sai.pre_paid_period = pre_paid_period
            sai.buy_start_time = startTime
            sai.buy_end_time = endTime
            sai.create_time = create_time
            sai.pre_paid_money = appCreateService.get_estimate_service_fee(sai, self.response_region)
            sai.region = self.response_region
            sai.save()
            # 创建订单
            if sai.pre_paid_money > 0:
                ServiceFeeBill.objects.create(tenant_id=tenant_id, service_id=service_id,
                                              prepaid_money=sai.pre_paid_money, pay_status="unpayed",
                                              cost_type="first_create", node_memory=min_memory, node_num=min_node,
                                              disk=disk, buy_period=pre_paid_period * 24 * 30,create_time=create_time,
                                              pay_time=create_time)

            # create console service
            service.desc = service_desc
            newTenantService = baseService.create_service(
                service_id, tenant_id, service_alias, service_cname, service, self.user.pk, region=self.response_region)
            monitorhook.serviceMonitor(self.user.nick_name, newTenantService, 'create_service', True)
            baseService.addServicePort(newTenantService, False, container_port=5000, protocol='http', port_alias='', is_inner_service=False, is_outer_service=True)

            # code repos
            # 自建git (gitlab_self)与gitlab_manual一样
            if service_code_from == "gitlab_self":
                service_code_from = "gitlab_manual"

            if service_code_from == "gitlab_new":
                codeRepositoriesService.initRepositories(self.tenant, self.user, newTenantService, service_code_from, "", "", "")
            elif service_code_from == "gitlab_exit":
                code_clone_url = request.POST.get("service_code_clone_url", "")
                code_id = request.POST.get("service_code_id", "")
                code_version = request.POST.get("service_code_version", "master")
                if code_id == "" or code_clone_url == "" or code_version == "":
                    data["status"] = "code_repos"
                    TenantServiceInfo.objects.get(service_id=service_id).delete()
                    return JsonResponse(data, status=200)
                codeRepositoriesService.initRepositories(self.tenant, self.user, newTenantService, service_code_from, code_clone_url, code_id, code_version)
            elif service_code_from == "gitlab_manual":
                code_clone_url = request.POST.get("service_code_clone_url", "")
                code_version = request.POST.get("service_code_version", "master")
                code_id = 0
                if code_clone_url == "" or code_version == "":
                    data["status"] = "code_repos"
                    TenantServiceInfo.objects.get(service_id=service_id).delete()
                    return JsonResponse(data, status=200)
                codeRepositoriesService.initRepositories(self.tenant, self.user, newTenantService, service_code_from, code_clone_url, code_id, code_version)
            elif service_code_from == "github":
                code_id = request.POST.get("service_code_id", "")
                code_clone_url = request.POST.get("service_code_clone_url", "")
                code_version = request.POST.get("service_code_version", "master")
                if code_id == "" or code_clone_url == "" or code_version == "":
                    data["status"] = "code_repos"
                    TenantServiceInfo.objects.get(service_id=service_id).delete()
                    return JsonResponse(data, status=200)
                codeRepositoriesService.initRepositories(self.tenant, self.user, newTenantService, service_code_from, code_clone_url, code_id, code_version)

            group_id = request.POST.get("select_group_id", "")
            # 创建关系
            if group_id != "":
                group_id = int(group_id)
                if group_id > 0:
                    ServiceGroupRelation.objects.create(service_id=service_id, group_id=group_id,
                                                        tenant_id=self.tenant.tenant_id, region_name=self.response_region)
            # create region tenantservice
            # 第一步创建时不在region上创建service,再第3步设置时再创建,否则第三部无法创建
            # baseService.create_region_service(newTenantService, self.tenantName, self.response_region, self.user.nick_name)
            monitorhook.serviceMonitor(self.user.nick_name, newTenantService, 'init_region_service', True)
            # create service env
            # baseService.create_service_env(tenant_id, service_id, self.response_region)
            # record log
            data["status"] = "success"
            data["service_alias"] = service_alias
            data["service_id"] = service_id
        except Exception as e:
            logger.exception("create console service failed!")
            logger.exception(e)
            tempTenantService = TenantServiceInfo.objects.get(service_id=service_id)
            codeRepositoriesService.deleteProject(tempTenantService)
            TenantServiceInfo.objects.filter(service_id=service_id).delete()
            TenantServiceAuth.objects.filter(service_id=service_id).delete()
            TenantServiceRelation.objects.filter(service_id=service_id).delete()
            ServiceGroupRelation.objects.filter(service_id=service_id)
            ServiceAttachInfo.objects.filter(service_id=service_id)
            monitorhook.serviceMonitor(self.user.nick_name, tempTenantService, 'create_service_error', False)
            data["status"] = "failure"
        return JsonResponse(data, status=200)


class AppWaitingCodeView(LeftSideBarMixin, AuthedView):

    def get_media(self):
        media = super(AuthedView, self).get_media() + self.vendor(
            'www/css/goodrainstyle.css', 'www/css/style.css', 'www/css/style-responsive.css', 'www/js/jquery.cookie.js',
            'www/js/common-scripts.js', 'www/js/jquery.dcjqaccordion.2.7.js', 'www/js/jquery.scrollTo.min.js',
            'www/js/respond.min.js')
        return media

    @never_cache
    @perm_required('create_service')
    def get(self, request, *args, **kwargs):
        try:
            context = self.get_context()
            context["myAppStatus"] = "active"
            context["tenantName"] = self.tenantName
            context["tenantService"] = self.service

            context["httpGitUrl"] = codeRepositoriesService.showGitUrl(self.service)

            if ServiceCreateStep.objects.filter(tenant_id=self.tenant.tenant_id,
                                                service_id=self.service.service_id).exists():
                ServiceCreateStep.objects.filter(tenant_id=self.tenant.tenant_id,
                                                 service_id=self.service.service_id).update(app_step=2)
            else:
                ServiceCreateStep.objects.create(tenant_id=self.tenant.tenant_id, service_id=self.service.service_id,
                                                 app_step=2)
        except Exception as e:
            logger.exception(e)
        return TemplateResponse(self.request, "www/app_create_step_2_waiting.html", context)


class AppSettingsView(LeftSideBarMixin,AuthedView,CopyPortAndEnvMixin):
    """服务设置"""
    def get_media(self):
        media = super(AuthedView, self).get_media() + self.vendor(
            'www/css/goodrainstyle.css', 'www/css/style.css', 'www/css/style-responsive.css', 'www/js/jquery.cookie.js',
            'www/js/common-scripts.js', 'www/js/jquery.dcjqaccordion.2.7.js', 'www/js/jquery.scrollTo.min.js',
            'www/js/respond.min.js')
        return media

    def save_ports_envs_and_volumes(self, ports, envs, volumes, tenant_serivce):
        """保存端口,环境变量和持久化目录"""
        for port in ports:
            baseService.addServicePort(tenant_serivce, False, container_port=int(port["container_port"]),
                                       protocol=port["protocol"], port_alias=port["port_alias"],
                                       is_inner_service=port["is_inner_service"],
                                       is_outer_service=port["is_outer_service"])

        for env in envs:
            baseService.saveServiceEnvVar(tenant_serivce.tenant_id, tenant_serivce.service_id, 0,
                                          env["name"], env["attr_name"], env["attr_value"], True, "inner")

        for volume in volumes:
            baseService.add_volume_list(tenant_serivce, volume["volume_path"])

    def saveAdapterEnv(self, service):
        num = TenantServiceEnvVar.objects.filter(service_id=service.service_id, attr_name="GD_ADAPTER").count()
        if num < 1:
            attr = {"tenant_id": service.tenant_id, "service_id": service.service_id, "name": "GD_ADAPTER",
                    "attr_name": "GD_ADAPTER", "attr_value": "true", "is_change": 0, "scope": "inner", "container_port":-1}
            TenantServiceEnvVar.objects.create(**attr)
            data = {"action": "add", "attrs": attr}
            regionClient.createServiceEnv(service.service_region, service.service_id, json.dumps(data))

    @never_cache
    @perm_required('create_service')
    def get(self, request, *args, **kwargs):
        try:
            context = self.get_context()
            context["myAppStatus"] = "active"
            context["tenantName"] = self.tenantName
            context["tenantService"] = self.service
            deployTenantServices = TenantServiceInfo.objects.filter(
                tenant_id=self.tenant.tenant_id,
                service_region=self.response_region,
                service_origin='assistant')

            openInnerServices = []
            for dts in deployTenantServices:
                if TenantServicesPort.objects.filter(service_id=dts.service_id,is_inner_service=True):
                    openInnerServices.append(dts)

            context["openInnerServices"] = openInnerServices
            context["service_envs"] = TenantServiceEnvVar.objects.filter(service_id=self.service.service_id, scope__in=("inner", "both")).exclude(container_port= -1)
            port_list = TenantServicesPort.objects.filter(service_id=self.service.service_id)
            context["service_ports"] = list(port_list)
            dpsids = []

            for hasService in openInnerServices:
                dpsids.append(hasService.service_id)
            hasTenantServiceEnvs = TenantServiceEnvVar.objects.filter(service_id__in=dpsids)
            # 已有服务的一个服务id对应一条服务的环境变量
            env_map = {env.service_id: env for env in list(hasTenantServiceEnvs)}
            context["env_map"] = env_map

            # 除当前服务外的所有的服务
            tenantServiceList = baseService.get_service_list(self.tenant.pk, self.user, self.tenant.tenant_id, region=self.response_region)
            context["tenantServiceList"] = tenantServiceList
            # 挂载目录
            mtsrs = TenantServiceMountRelation.objects.filter(service_id=self.service.service_id)
            mntsids = []
            if len(mtsrs) > 0:
                for mnt in mtsrs:
                    mntsids.append(mnt.dep_service_id)
            context["mntsids"] = mntsids

            ServiceCreateStep.objects.filter(service_id=self.service.service_id,tenant_id=self.tenant.tenant_id).update(app_step=3)

        except Exception as e:
            logger.exception(e)
        return TemplateResponse(self.request, "www/app_create_step_3_setting.html", context)

    @never_cache
    @perm_required('create_service')
    def post(self, request, *args, **kwargs):
        data = {}
        service_alias_list = []
        init_region = False
        try:
            # 端口信息
            port_list = json.loads(request.POST.get("port_list", "[]"))
            # 环境变量信息
            env_list = json.loads(request.POST.get("env_list", "[]"))
            # 持久化目录信息
            volume_list = json.loads(request.POST.get("volume_list", "[]"))
            # 依赖服务id
            depIds = json.loads(request.POST.get("depend_list", "[]"))
            # 挂载其他服务目录
            service_alias_list = json.loads(request.POST.get("mnt_list","[]"))

            # 将刚开始创建的5000端口删除
            previous_list = map(lambda containerPort: containerPort["container_port"], port_list)
            # 处理用户自定义的port
            if len(previous_list) == 0:
                TenantServicesPort.objects.filter(service_id=self.service.service_id).delete()
            else:
                delete_default_port = True
                for tmp_port in previous_list:
                    if tmp_port == 5000:
                        delete_default_port = False
                        continue
                if delete_default_port:
                    TenantServicesPort.objects.filter(service_id=self.service.service_id,
                                                      container_port=5000).delete()

            newTenantService = TenantServiceInfo.objects.get(tenant_id=self.tenant.tenant_id,
                                                             service_id=self.service.service_id)
            self.save_ports_envs_and_volumes(port_list, env_list, volume_list, newTenantService)
            # 创建挂载目录
            for dep_service_alias in service_alias_list:
                baseService.create_service_mnt(self.tenant.tenant_id, self.service.service_id, dep_service_alias["otherName"],
                                               self.service.service_region)

            baseService.create_region_service(newTenantService, self.tenantName, self.response_region,
                                              self.user.nick_name)
            init_region = True

            logger.debug(depIds)
            if len(depIds) > 0:
                # 检查当前服务是否有GDADAPTER参数
                # self.saveAdapterEnv(newTenantService)
                for sid in depIds:
                    try:
                        baseService.create_service_dependency(self.tenant.tenant_id, self.service.service_id, sid, self.response_region)
                    except Exception as e:
                        logger.exception(e)

            data["status"] = "success"

        except Exception as e:
            TenantServiceEnvVar.objects.filter(service_id=self.service.service_id).delete()
            TenantServiceVolume.objects.filter(service_id=self.service.service_id).delete()
            TenantServiceMountRelation.objects.filter(service_id=self.service.service_id).delete()
            if len(service_alias_list) > 0:
                for dep_service_alias in service_alias_list:
                    baseService.cancel_service_mnt(self.tenant.tenant_id, self.service.service_id, dep_service_alias,
                                                   self.service.service_region)
            if init_region:
                regionClient.delete(self.service.service_region, self.service.service_id)
            logger.exception(e)
            logger.error("AppSettingsView create service error!")
            data["status"] = "failure"
        return JsonResponse(data,status=200)


class AppLanguageCodeView(LeftSideBarMixin, AuthedView):

    def get_media(self):
        media = super(AuthedView, self).get_media() + self.vendor(
            'www/css/goodrainstyle.css', 'www/css/style.css', 'www/css/style-responsive.css', 'www/js/jquery.cookie.js',
            'www/js/common-scripts.js', 'www/js/jquery.dcjqaccordion.2.7.js', 'www/js/jquery.scrollTo.min.js',
            'www/js/respond.min.js', 'www/js/app-language.js')
        return media

    @never_cache
    @perm_required('create_service')
    def get(self, request, *args, **kwargs):
        language = "none"
        context = self.get_context()
        try:
            if self.service.language == "" or self.service.language is None:
                return self.redirect_to('/apps/{0}/{1}/app-waiting/'.format(self.tenant.tenant_name, self.service.service_alias))

            tenantServiceEnv = TenantServiceEnv.objects.get(service_id=self.service.service_id)
            if tenantServiceEnv.user_dependency is not None and tenantServiceEnv.user_dependency != "":
                return self.redirect_to('/apps/{0}/{1}/detail/'.format(self.tenant.tenant_name, self.service.service_alias))

            context["myAppStatus"] = "active"
            context["tenantName"] = self.tenantName
            context["tenantService"] = self.service
            language = self.service.language
            data = json.loads(tenantServiceEnv.check_dependency)
            context["dependencyData"] = data
            redirectme = is_redirect(self.service.language, data)
            context["redirectme"] = redirectme
            if redirectme:
                language = "default"
        except Exception as e:
            logger.exception(e)
        if self.service.language == 'docker':
            self.service.cmd = ''
            self.service.save()
            regionClient.update_service(self.response_region, self.service.service_id, {"cmd": ""})
            return TemplateResponse(self.request, "www/app_create_step_4_default.html", context)
        ServiceCreateStep.objects.filter(service_id=self.service.service_id,tenant_id=self.tenant.tenant_id).update(app_step=4)
        return TemplateResponse(self.request, "www/app_create_step_4_" + language.replace(".", "").lower() + ".html", context)

    def memory_choices(self, free=False):
        memory_dict = {}
        key_list = []
        key_list.append("128")
        key_list.append("256")
        key_list.append("512")
        key_list.append("1024")
        memory_dict["128"] = '128M'
        memory_dict["256"] = '256M'
        memory_dict["512"] = '512M'
        memory_dict["1024"] = '1G'
        if not free:
            key_list.append("2048")
            key_list.append("4096")
            key_list.append("8192")
            key_list.append("16384")
            key_list.append("32768")
            key_list.append("65536")
            memory_dict["2048"] = '2G'
            memory_dict["4096"] = '4G'
            memory_dict["8192"] = '8G'
            memory_dict["16384"] = '16G'
            memory_dict["32768"] = '32G'
            memory_dict["65536"] = '64G'
        return memory_dict, key_list

    @never_cache
    @perm_required('create_service')
    def post(self, request, *args, **kwargs):
        data = {}
        try:
            service_version = request.POST.get("service_version", "")
            service_server = request.POST.get("service_server", "")
            service_dependency = request.POST.get("service_dependency", "")
            logger.debug(service_dependency)
            checkJson = {}
            checkJson["language"] = self.service.language
            if service_version != "":
                checkJson["runtimes"] = service_version
            else:
                checkJson["runtimes"] = ""
            if service_server != "":
                checkJson["procfile"] = service_server
            else:
                checkJson["procfile"] = ""
            if service_dependency != "":
                dps = service_dependency.split(",")
                d = {}
                for dp in dps:
                    if dp is not None and dp != "":
                        d["ext-" + dp] = "*"
                checkJson["dependencies"] = d
            else:
                checkJson["dependencies"] = {}

            tenantServiceEnv = TenantServiceEnv.objects.get(service_id=self.service.service_id)
            tenantServiceEnv.user_dependency = json.dumps(checkJson)
            tenantServiceEnv.save()

            # docker构建时自定义内存逻辑
            if self.service.language == 'docker':
                try:
                    memory_str = request.POST.get("service_memory", "128")
                    service_memory = int(memory_str)
                    if service_memory != 128:
                        self.service.min_memory = service_memory
                        self.service.save()
                        regionClient.update_service(self.response_region,
                                                    self.service.service_id,
                                                    {"memory": memory_str})
                except Exception as e:
                    logger.error("docker build memory config failed")
                    logger.exception(e)

            data["status"] = "success"
            # 设置服务购买的起始时间
            attach_info = ServiceAttachInfo.objects.get(service_id=self.service.service_id)
            pre_paid_period = attach_info.pre_paid_period
            if self.tenant.pay_type == "free":
                # 免费租户的应用过期时间为7天
                startTime = datetime.datetime.now() + datetime.timedelta(days=7)+datetime.timedelta(hours=1)
                startTime = startTime.strftime("%Y-%m-%d %H:00:00")
                startTime = datetime.datetime.strptime(startTime, "%Y-%m-%d %H:%M:%S")
                service = self.service
                service.expired_time = startTime
                service.save()
                endTime = startTime + relativedelta(months=int(pre_paid_period))
                ServiceAttachInfo.objects.filter(service_id=self.service.service_id).update(buy_start_time=startTime,
                                                                                            buy_end_time=endTime)
            else:
                # 付费用户一个小时调试
                startTime = datetime.datetime.now() + datetime.timedelta(hours=2)
                startTime = startTime.strftime("%Y-%m-%d %H:00:00")
                startTime = datetime.datetime.strptime(startTime, "%Y-%m-%d %H:%M:%S")
                endTime = startTime + relativedelta(months=int(pre_paid_period))
                ServiceAttachInfo.objects.filter(service_id=self.service.service_id).update(buy_start_time=startTime,
                                                                                            buy_end_time=endTime)
            ServiceCreateStep.objects.filter(service_id=self.service.service_id,tenant_id=self.tenant.tenant_id).delete()
        except Exception as e:
            logger.exception(e)
            data["status"] = "failure"
        return JsonResponse(data, status=200)


class GitLabWebHook(BaseView):

    @never_cache
    def post(self, request, *args, **kwargs):
        result = {}
        try:
            payload = request.body
            payloadJson = json.loads(payload)
            project_id = payloadJson["project_id"]
            repositoryJson = payloadJson["repository"]
            name = repositoryJson["name"]
            git_url = repositoryJson["git_http_url"]
            logger.debug(str(project_id) + "==" + name + "==" + git_url)
            listTs = TenantServiceInfo.objects.filter(git_project_id=project_id).exclude(code_from="github")
            for ts in listTs:
                codeRepositoriesService.codeCheck(ts)
            result["status"] = "success"
        except Exception as e:
            logger.exception(e)
            result["status"] = "failure"
        return HttpResponse(json.dumps(result))


class GitHubWebHook(BaseView):

    @never_cache
    def post(self, request, *args, **kwargs):
        result = {}
        try:
            # event = request.META['HTTP_X_GITHUB_EVENT']
            # logger.debug(event)
            payload = request.body
            payloadJson = json.loads(payload)
            repositoryJson = payloadJson["repository"]
            fullname = repositoryJson["full_name"]
            git_url = repositoryJson["clone_url"]
            project_id = repositoryJson["id"]
            logger.debug(str(project_id) + "==" + fullname + "==" + git_url)
            listTs = TenantServiceInfo.objects.filter(git_project_id=project_id, code_from="github")
            for ts in listTs:
                codeRepositoriesService.codeCheck(ts)
            result["status"] = "success"
        except Exception as e:
            logger.exception(e)
            result["status"] = "failure"
        return HttpResponse(json.dumps(result))


class GitCheckCode(BaseView):

    @never_cache
    def get(self, request, *args, **kwargs):
        data = {}
        try:
            service_id = request.GET.get("service_id", "")
            logger.debug("git code request: " + service_id)
            if service_id is not None and service_id != "":
                tse = TenantServiceEnv.objects.get(service_id=service_id)
                result = tse.user_dependency
                if result is not None and result != "":
                    data = json.loads(result)
        except Exception as e:
            logger.exception(e)
        return JsonResponse(data, status=200)

    @never_cache
    def post(self, request, *args, **kwargs):
        result = {}
        try:
            service_id = request.POST.get("service_id", "")
            dependency = request.POST.get("condition", "")
            logger.debug(service_id + "=" + dependency)
            if service_id is not None and service_id != "" and dependency != "":
                dps = json.loads(dependency)
                language = dps["language"]
                if language is not None and language != "" and language != "no":
                    try:
                        tse = TenantServiceEnv.objects.get(service_id=service_id)
                        tse.language = language
                        tse.check_dependency = dependency
                        tse.save()
                    except Exception:
                        tse = TenantServiceEnv(service_id=service_id, language=language, check_dependency=dependency)
                        tse.save()
                    service = TenantServiceInfo.objects.get(service_id=service_id)
                    if language != "false":
                        if language.find("Java") > -1 and service.min_memory < 512:
                            service.min_memory = 512
                            data = {}
                            data["language"] = "java"
                            regionClient.changeMemory(service.service_region, service_id, json.dumps(data))
                        service.language = language
                        service.save()
            result["status"] = "success"
        except Exception as e:
            logger.exception(e)
            result["status"] = "failure"
        return HttpResponse(json.dumps(result))
