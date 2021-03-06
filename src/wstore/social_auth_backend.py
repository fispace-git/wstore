# -*- coding: utf-8 -*-

# Copyright (c) 2013 CoNWeT Lab., Universidad Politécnica de Madrid

# This file is part of WStore.

# WStore is free software: you can redistribute it and/or modify
# it under the terms of the European Union Public Licence (EUPL)
# as published by the European Commission, either version 1.1
# of the License, or (at your option) any later version.

# WStore is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# European Union Public Licence for more details.

# You should have received a copy of the European Union Public Licence
# along with WStore.
# If not, see <https://joinup.ec.europa.eu/software/page/eupl/licence-eupl>.

"""
FI-WARE IdM OAuth2 support.

This contribution adds support for FI-WARE IdM OAuth2 service. The settings
FIWARE_APP_ID and FIWARE_API_SECRET must be defined with the values
given by FI-WARE IdM application registration process.

Extended permissions are supported by defining FIWARE_EXTENDED_PERMISSIONS
setting, it must be a list of values to request.

By default account id and token expiration time are stored in extra_data
field, check OAuthBackend class for details on how to extend it.
"""
from urllib import urlencode
from urlparse import urljoin

from django.utils import simplejson
from django.conf import settings

from social_auth.utils import dsa_urlopen
from social_auth.backends import BaseOAuth2, OAuthBackend
from wstore.models import Organization
from wstore.store_commons.utils.method_request import MethodRequest
import json
import urllib2
import jwt

# idm configuration
#FIWARE_AUTHORIZATION_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/realms/master/tokens/login')
#FIWARE_ACCESS_TOKEN_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/realms/master/tokens/access/codes')
#FIWARE_USER_DATA_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/realms/master/account')
#FIWARE_NOTIFICATION_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/purchases')
#FIWARE_APPLICATIONS_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/applications.json')
#FIWARE_LOGOUT_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/realms/master/tokens/logout')

FIWARE_AUTHORIZATION_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/realms/fispace/tokens/login')
FIWARE_ACCESS_TOKEN_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/realms/fispace/tokens/access/codes')
FIWARE_USER_DATA_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/realms/fispace/account')
FIWARE_NOTIFICATION_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/purchases')
FIWARE_APPLICATIONS_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/admin/realms/fispace/applications')
FIWARE_LOGOUT_URL = urljoin(settings.FIWARE_IDM_ENDPOINT, '/auth/realms/fispace/tokens/logout')

FIWARE_PROVIDER_ROLE = 'Provider'
FIWARE_CUSTOMER_ROLE = 'Customer'
FIWARE_DEVELOPER_ROLE = 'Developer'



class FiwareBackend(OAuthBackend):
    """FI-WARE IdM OAuth authentication backend"""
    name = 'fiware'
    # Default extra data to store
    EXTRA_DATA = [
        ('nickName', 'username'),
    ]

    def get_user_id(self, details, response):
        """Return the user id, FI-WARE IdM only provides username as a unique
        identifier"""
        return details['username']

    def get_user_details(self, response):
        """Return user details from FI-WARE account"""
        return {'username': response.get('preferred_username'),
                'email': response.get('email') or '',
                'fullname': response.get('name') or ''}


class FiwareAuth(BaseOAuth2):
    """FI-WARE OAuth2 mechanism"""
    AUTHORIZATION_URL = FIWARE_AUTHORIZATION_URL
    ACCESS_TOKEN_URL = FIWARE_ACCESS_TOKEN_URL
    AUTH_BACKEND = FiwareBackend
    REDIRECT_STATE = False
    STATE_PARAMETER = True
    SETTINGS_KEY_NAME = 'FIWARE_APP_ID'
    SETTINGS_SECRET_NAME = 'FIWARE_API_SECRET'
    SCOPE_SEPARATOR = ','
    # Look at http://developer.github.com/v3/oauth/
    SCOPE_VAR_NAME = 'FIWARE_EXTENDED_PERMISSIONS'

    FIWARE_ORGANIZATION = getattr(settings, 'FIWARE_ORGANIZATION', None)

    def user_data(self, access_token, *args, **kwargs):
        """Loads user data from service"""
        data = jwt.decode( access_token, verify=False )

        return data


def fill_internal_user_info(*arg, **kwargs):

    # Update user info
    response = kwargs['response']

    # This roles will be user organization roles
    roles = response['realm_access']['roles']
    wstore_roles = []

    # Save the current access token for future calls
    kwargs['user'].userprofile.access_token = response['access_token']
    
    kwargs['user'].userprofile.actor_id = response['preferred_username']

    if 'refresh_token' in response:
        kwargs['user'].userprofile.refresh_token = response['refresh_token']

        social = kwargs['user'].social_auth.filter(provider='fiware')[0]
        social.extra_data['refresh_token'] = response['refresh_token']
        social.save()

    kwargs['user'].userprofile.complete_name = response['name']
    kwargs['user'].userprofile.save()

    # Get user private organization
    # user_org = Organization.objects.filter(name=kwargs['user'].username)
    user_org = Organization.objects.filter(actor_id=kwargs['user'].userprofile.actor_id)
    if len(user_org) == 0:
        user_org = Organization.objects.get(name=kwargs['user'].username)
        user_org.actor_id = kwargs['user'].userprofile.actor_id
        user_org.save()
    else:
        user_org = user_org[0]

    kwargs['user'].userprofile.current_organization = user_org
    kwargs['user'].userprofile.save()

    # Include new roles in the private organization
    for role in roles:
        wstore_roles.append(role)

    # Check if the user is an admin
    if 'Provider' in wstore_roles and not kwargs['user'].is_staff:
        kwargs['user'].is_staff = True
        kwargs['user'].save()

    elif not 'Provider' in wstore_roles and kwargs['user'].is_staff:
        kwargs['user'].is_staff = False
        kwargs['user'].save()

    organizations = []
    user_roles = ['customer', 'provider']

    organizations.append({
        'organization': user_org.pk,
        'roles': user_roles
    })

    #if we want to use the user token, we need to make sure that the token is valid (refresh if necessary) in order to refresh token
    #we need a valir bearer token to begin with. Therefore this method won't work with username workaround
    headers = {'Authorization': 'Bearer ' + kwargs['user'].userprofile.access_token}
    
    opener = urllib2.build_opener()
    request = MethodRequest('GET', settings.AIL_URL+'/api/users/' + kwargs['user'].username + '/companies', '', headers)
    

    try:
        response = opener.open(request)
        data = json.loads(response.read())
    except Exception, e:
        data = []
    try:
        # Check organizations info
        idm_organizations = []
        if 'companyList' in data:
            for org in data['companyList']:
                idm_organizations.append(org)

        for org in idm_organizations:
            # Check if the organization exist
            org_model = Organization.objects.filter(actor_id=org['id'])

            if len(org_model) == 0:
                # Create the organization
                org_model = Organization.objects.create(
                    name=org['name'],
                    private=False,
                    actor_id=org['id']
                )
            else:
                org_model = org_model[0]

            # Check organization roles
            idm_org_roles = org['companyRoles']
            org_roles = []

            for role in idm_org_roles:
                if role['name'] == 'Owner':
                    if not kwargs['user'].pk in org_model.managers:
                        org_model.managers.append(kwargs['user'].pk)
                elif role['name'] == FIWARE_PROVIDER_ROLE:
                    org_roles.append('provider')
                elif role['name'] == FIWARE_CUSTOMER_ROLE:
                    org_roles.append('customer')
                elif role['name'] == FIWARE_DEVELOPER_ROLE:
                    org_roles.append('developer')

            organizations.append({
                'organization': org_model.pk,
                'roles': org_roles
            })

        kwargs['user'].userprofile.organizations = organizations
        kwargs['user'].userprofile.save()
    except Exception as e:
       import sys
       tb = sys.exc_info()[2]
       print str(e), tb.tb_lineno


# Backend definition
BACKENDS = {
    'fiware': FiwareAuth,
}
