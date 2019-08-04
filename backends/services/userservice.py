# -*- coding: utf8 -*-
import logging
import re

from django.db.models import Q
from fuzzyfinder.main import fuzzyfinder

from backends.services.exceptions import PasswordTooShortError
from backends.services.exceptions import TenantNotExistError
from backends.services.exceptions import UserNotExistError
from console.services.user_services import user_services as console_user_service
from www.gitlab_http import GitlabApi
from www.models.main import PermRelTenant
from www.models.main import Tenants
from www.models.main import Users
from www.tenantservice.baseservice import CodeRepositoriesService

logger = logging.getLogger("default")
codeRepositoriesService = CodeRepositoriesService()
gitClient = GitlabApi()


class UserService(object):
    def check_params(self, user_name, email, password, re_password):
        is_pass, msg = self.__check_user_name(user_name)
        if not is_pass:
            return is_pass, msg
        is_pass, msg = self.__check_email(email)
        if not is_pass:
            return is_pass, msg

        # is_pass, msg = self.__check_phone(phone)
        # if not is_pass:
        #     return is_pass, msg

        if password != re_password:
            return False, "两次输入的密码不一致"
        return True, "success"

    def __check_user_name(self, user_name):
        if not user_name:
            return False, "用户名不能为空"
        if console_user_service.is_user_exist(user_name):
            return False, "用户{0}已存在".format(user_name)
        r = re.compile(u'^[a-zA-Z0-9_\\-\u4e00-\u9fa5]+$')
        if not r.match(user_name.decode("utf-8")):
            return False, "用户名称只支持中英文下划线和中划线"
        return True, "success"

    def __check_email(self, email):
        if not email:
            return False, "邮箱不能为空"
        if console_user_service.get_user_by_phone(email):
            return False, "邮箱{0}已存在".format(email)
        r = re.compile(r'^[\w\-\.]+@[\w\-]+(\.[\w\-]+)+$')
        if not r.match(email):
            return False, "邮箱地址不合法"
        return True, "success"

    def create_user(self, user_name, phone, email, raw_password, rf, enterprise, client_ip):
        user = Users.objects.create(
            nick_name=user_name,
            email=email,
            phone=phone,
            sso_user_id="",
            enterprise_id=enterprise.enterprise_id,
            is_active=False,
            rf=rf,
            client_ip=client_ip)
        user.set_password(raw_password)
        return user

    def delete_user(self, user_id):
        user = Users.objects.get(user_id=user_id)
        git_user_id = user.git_user_id

        PermRelTenant.objects.filter(user_id=user.pk).delete()
        gitClient.deleteUser(git_user_id)
        user.delete()

    def update_user_password(self, user_id, new_password):
        user = Users.objects.get(user_id=user_id)
        if len(new_password) < 8:
            raise PasswordTooShortError("密码不能小于8位")
        user.set_password(new_password)
        user.save()
        # 同时修改git的密码
        codeRepositoriesService.modifyUser(user, new_password)

    def get_user_tenants(self, user_id):
        tenant_id_list = PermRelTenant.objects.filter(user_id=user_id).values_list("tenant_id", flat=True)
        tenant_list = Tenants.objects.filter(pk__in=tenant_id_list).values_list("tenant_alias", flat=True)
        return tenant_list

    def get_all_users(self):
        user_list = Users.objects.all().order_by("-create_time")
        return user_list

    def get_client_ip(self, request):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR')
        return ip

    def get_fuzzy_users(self, tenant_name, user_name):
        # 如果租户名存在
        if tenant_name:
            tenants = Tenants.objects.filter(tenant_name=tenant_name)
            if not tenants:
                raise TenantNotExistError("租户{}不存在".format(tenant_name))
            tenant = tenants[0]
            user_id_list = PermRelTenant.objects.filter(tenant_id=tenant.ID).values_list("user_id", flat=True)
            user_list = Users.objects.filter(user_id__in=user_id_list)
            user_name_list = map(lambda x: x.nick_name.lower(), user_list)

        else:
            user_name_map = list(Users.objects.values("nick_name"))
            user_name_list = map(lambda x: x.get("nick_name").lower(), user_name_map)

        find_user_name = list(fuzzyfinder(user_name.lower(), user_name_list))
        user_query = Q(nick_name__in=find_user_name)
        user_list = Users.objects.filter(user_query)
        return user_list

    def batch_delete_users(self, tenant_name, user_id_list):

        tenant = Tenants.objects.get(tenant_name=tenant_name)
        PermRelTenant.objects.filter(user_id__in=user_id_list, tenant_id=tenant.ID).delete()

    def get_user_by_username(self, user_name):

        users = Users.objects.filter(nick_name=user_name)
        if not users:
            raise UserNotExistError("用户名{}不存在".format(user_name))

        return users[0]

    def is_user_exist(self, user_name):
        try:
            self.get_user_by_username(user_name)
            return True
        except UserNotExistError:
            return False

    def get_by_username_or_phone_or_email(self, query_condition):
        users = Users.objects.filter(
            Q(nick_name__contains=query_condition) | Q(phone__contains=query_condition)
            | Q(email__contains=query_condition)).order_by("-create_time")
        return users

    def get_user_by_user_id(self, user_id):
        u = Users.objects.filter(user_id=user_id)
        if not u:
            raise UserNotExistError("用户{}不存在".format(user_id))
        return u[0]

    def get_creater_by_user_id(self, user_id):
        u = Users.objects.filter(user_id=user_id)
        if not u:
            return 0
        return u[0]


user_service = UserService()
