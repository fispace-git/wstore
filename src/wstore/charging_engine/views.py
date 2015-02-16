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

import json
from bson import ObjectId

from django.conf import settings
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator

from wstore.store_commons.resource import Resource
from wstore.store_commons.utils.http import build_response, supported_request_mime_types
from wstore.models import Purchase
from wstore.models import UserProfile
from wstore.charging_engine.charging_engine import ChargingEngine
from wstore.contracting.purchase_rollback import rollback
from wstore.contracting.notify_provider import notify_provider
from wstore.store_commons.database import get_database_connection


class ServiceRecordCollection(Resource):

    # This method is used to load SDR documents and
    # start the charging process
    @supported_request_mime_types(('application/json',))
    def create(self, request, reference):
        try:
            # Extract SDR document from the HTTP request
            data = json.loads(request.raw_post_data)

            # Validate SDR structure
            if not 'offering' in data or not 'customer' in data or not 'time_stamp' in data \
            or not 'correlation_number' in data or not 'record_type' in data or not 'unit' in data \
            or not 'value' in data or not 'component_label' in data:
                raise Exception('Invalid JSON content')

            # Get the purchase
            purchase = Purchase.objects.get(ref=reference)
            # Call the charging engine core with the SDR
            charging_engine = ChargingEngine(purchase)
            charging_engine.include_sdr(data)
        except Exception, e:
            return build_response(request, 400, e.message)

        # Return response
        return build_response(request, 200, 'OK')


class PayPalConfirmation(Resource):

    # This method is used to receive the PayPal confirmation
    # when the customer is paying using his PayPal account
    @method_decorator(login_required)
    def read(self, request, reference):
        purchase = None
        try:
            token = request.GET.get('token')
            payer_id = request.GET.get('PayerID', '')

            db = get_database_connection()

            # Uses an atomic operation to get and set the _lock value in the purchase
            # document
            pre_value = db.wstore_purchase.find_and_modify(
                query={'_id': ObjectId(reference)},
                update={'$set': {'_lock': True}}
            )

            # If the value of _lock before setting it to true was true, means
            # that the time out function has acquired it previously so the
            # view ends
            if '_lock' in pre_value and pre_value['_lock']:
                raise Exception('')

            purchase = Purchase.objects.get(ref=reference)

            # Check that the request user is authorized to end the payment
            if request.user.userprofile.current_organization != purchase.owner_organization:
                raise Exception()

            # If the purchase state value is different from pending means that
            # the timeout function has completely ended before acquire the resource
            # so _lock is set to false and the view ends
            if purchase.state != 'pending':
                db.wstore_purchase.find_and_modify(
                    query={'_id': ObjectId(reference)},
                    update={'$set': {'_lock': False}}
                )
                raise Exception('')

            pending_info = purchase.contract.pending_payment

            # Get the payment client
            # Load payment client
            cln_str = settings.PAYMENT_CLIENT
            client_class = cln_str.split('.')[-1]
            client_package = cln_str.partition('.' + client_class)[0]

            payment_client = getattr(__import__(client_package, globals(), locals(), [client_class], -1), client_class)

            # build the payment client
            client = payment_client(purchase)
            client.end_redirection_payment(token, payer_id)

            charging_engine = ChargingEngine(purchase)
            accounting = None
            if 'accounting' in pending_info:
                accounting = pending_info['accounting']

            charging_engine.end_charging(pending_info['price'], pending_info['concept'], pending_info['related_model'], accounting)
        except:
            # Rollback the purchase if existing
            if purchase is not None:
                rollback(purchase)

            context = {
                'title': 'Payment Canceled',
                'message': 'Your payment has been canceled. An error occurs or the timeout has finished, if you want to acquire the offering purchase it again in WStore.'
            }
            return render(request, 'err_msg.html', context)

        # Check if is the first payment
        if len(purchase.contract.charges) == 1:

            if purchase.organization_owned:
                org = purchase.owner_organization
                org.offerings_purchased.append(purchase.offering.pk)
                org.save()
            else:
                # Add the offering to the user profile
                user_profile = UserProfile.objects.get(user=purchase.customer)
                user_profile.offerings_purchased.append(purchase.offering.pk)
                user_profile.save()

            notify_provider(purchase)

        # _lock is set to false
        db.wstore_purchase.find_and_modify(
            query={'_id': reference},
            update={'$set': {'_lock': False}}
        )

        # Return the confirmation web page
        context = {
            'title': 'Payment Confirmed',
            'message': 'Your payment has been received. To download the resources and the invoice go to the offering details page.'
        }
        return render(request, 'err_msg.html', context)


class PayPalCancelation(Resource):

    # This method is used when the user cancel a charge
    # when is using a PayPal account
    @method_decorator(login_required)
    def read(self, request, reference):
        # In case the user cancels the payment is necessary to update
        # the database in order to avoid an inconsistent state
        try:
            purchase = Purchase.objects.get(pk=reference)

            # Check that the request user is authorized to end the payment
            if purchase.organization_owned:
                if request.user.userprofile.current_organization != purchase.owner_organization:
                    raise Exception()
            else:
                if request.user != purchase.customer:
                    raise Exception('')
            rollback(purchase)
        except:
            return build_response(request, 400, 'Invalid request')

        context = {
            'title': 'Payment Canceled',
            'message': 'Your payment has been canceled. If you want to acquire the offering purchase it again in WStore.'
        }
        return render(request, 'err_msg.html', context)
