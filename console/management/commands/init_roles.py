#!/usr/bin/env python
# -*- coding: utf-8 -*-

from django.core.management import BaseCommand
from django.db import transaction

from console.exception.main import ServiceHandleException
from console.services.perm_services import role_kind_services
from console.services.perm_services import user_kind_role_service
from console.repositories.team_repo import team_repo

from www.models.main import Tenants


class Command(BaseCommand):
    help = u'初始化所有团队的角色和团队成员的角色分配'

    @transaction.atomic()
    def handle(self, *args, **options):
        teams = Tenants.objects.all()
        for team in teams:
            role_kind_services.init_default_roles(kind="team", kind_id=team.tenant_id)
            users = team_repo.get_tenant_users_by_tenant_ID(team.ID)
            admin = role_kind_services.get_role_by_name(kind="team", kind_id=team.tenant_id, name="admin")
            developer = role_kind_services.get_role_by_name(kind="team", kind_id=team.tenant_id, name="developer")
            if not admin or not developer:
                raise ServiceHandleException(msg="init failed", msg_show=u"初始化失败")
            if users:
                for user in users:
                    if user.user_id == team.creater:
                        user_kind_role_service.update_user_roles(kind="team", kind_id=team.tenant_id, user=user, role_ids=[admin.ID])
                    else:
                        user_kind_role_service.update_user_roles(kind="team", kind_id=team.tenant_id, user=user, role_ids=[developer.ID])
        print ("初始化平台默认权限分配成功")