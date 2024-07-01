# This file is part of the Indico plugins.
# Copyright (C) 2017 - 2024 Max Fischer, Martin Claus, CERN
#
# The Indico plugins are free software; you can redistribute
# them and/or modify them under the terms of the MIT License;
# see the LICENSE file for more details.

import json
import time
from urllib.parse import urljoin

import requests
from flask import flash, redirect, request
from requests import RequestException
from werkzeug.exceptions import BadRequest, NotFound

from indico.core.plugins import url_for_plugin
from indico.modules.events.payment.controllers import RHPaymentBase
from indico.modules.events.payment.models.transactions import TransactionAction
from indico.modules.events.payment.notifications import notify_amount_inconsistency
from indico.modules.events.payment.util import TransactionStatus, get_active_payment_plugins, register_transaction
from indico.modules.events.registration.models.registrations import Registration
from indico.web.flask.util import url_for
from indico.web.rh import RH

from indico_payment_govukpay import _
from indico_payment_govukpay.plugin import GovukpayPaymentPlugin
from indico_payment_govukpay.util import to_small_currency, PROVIDER_GOVUKPAY, GOVUKPAY_INIT_URL, GOVUKPAY_API_TOKEN


class RHGovukPayBase(RHPaymentBase):

    def _process(self):
        payment_id = self.registration.transaction.data["payment_id"]
        return _process_payment_confirmation(self, payment_id)
        
    def _process_payment_confirmation(self, payment_id):
        endpoint = urljoin(GovukpayPaymentPlugin.settings.get('url'), GOVUKPAY_INIT_URL)
        headers = {'Authorization': f'Bearer {GOVUKPAY_API_TOKEN}', 'Content-Type': 'application/json'}
        response_json = requests.get(f'{endpoint}/{payment_id}', headers=headers).json()

        payment_finished = response_json['state']['finished']
        payment_status = response_json['state']['status']

        if not payment_finished:
            flash(_(f'Your payment is still processing. If the "Pending" payment status does not update, please contact the event organisers.'), 'warning')
            return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))

        if payment_status == 'success':
            return redirect(url_for_plugin('payment_govukpay.success', self.registration.locator.uuid, _external=True))
        elif payment_status == 'failed':
            return redirect(url_for_plugin('payment_govukpay.failure', self.registration.locator.uuid, _external=True))
        elif payment_status == 'cancelled':
            return redirect(url_for_plugin('payment_govukpay.cancel', self.registration.locator.uuid, _external=True))
        else:
            flash(_(f'Your payment could not be confirmed. Please contact the event organisers.'), 'warning')
            return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))

    # def _register_payment_successful(self):
    #     """Register the transaction as paid."""
    #     register_transaction(
    #         self.registration,
    #         self.registration.transaction.amount,
    #         self.registration.transaction.currency,
    #         TransactionAction.complete,
    #         PROVIDER_GOVUKPAY,
    #     )
    #
    # def _register_payment_successful(self):
    #     """Register the transaction as paid."""
    #     register_transaction(
    #         self.registration,
    #         self.registration.transaction.amount,
    #         self.registration.transaction.currency,
    #         TransactionAction.complete,
    #         PROVIDER_GOVUKPAY,
    #     )

class RHInitGovukpayPayment(RHGovukPayBase):
    def _get_transaction_parameters(self):
    #     """Get parameters for creating a transaction request."""
        settings = GovukpayPaymentPlugin.event_settings.get_all(self.event)
        format_map = {
            'user_id': self.registration.user_id,
            'user_name': self.registration.full_name,
            'user_firstname': self.registration.first_name,
            'user_lastname': self.registration.last_name,
            'event_id': self.registration.event_id,
            'event_title': self.registration.event.title,
            'registration_id': self.registration.id,
            'regform_title': self.registration.registration_form.title
        }
        description = settings['description'].format(**format_map)
        reference_prefix = settings['reference_prefix'].format(**format_map)
    #     order_identifier = settings['order_identifier'].format(**format_map)
    #     # see the SIXPay Manual
    #     # https://saferpay.github.io/jsonapi/#Payment_v1_PaymentPage_Initialize
    #     # on what these things mean
        transaction_parameters = {
            'amount': to_small_currency(self.registration.price, self.registration.currency),
            'reference': f'{reference_prefix}_E{self.registration.event_id}_R{self.registration.id}',
            'description': description,
            'language': 'en',
            'delayed_capture': False,
            'return_url': url_for_plugin('payment_govukpay.query', self.registration.locator.uuid, _external=True),
        }
        return transaction_parameters


    def _init_payment_page(self, transaction_data):
        """Initialize payment page."""
        endpoint = urljoin(GovukpayPaymentPlugin.settings.get('url'), GOVUKPAY_INIT_URL)
        headers = {'Authorization': f'Bearer {GOVUKPAY_API_TOKEN}', 'Content-Type': 'application/json'}
        resp = requests.post(endpoint, json=transaction_data, headers=headers)
        try:
            resp.raise_for_status()
        except RequestException as exc:
            GovukpayPaymentPlugin.logger.error('Could not initialize payment: %s', exc.response.text)
            raise Exception('Could not initialize payment')
        return resp.json()


    def _process(self):
        transaction_params = self._get_transaction_parameters()
        init_response = self._init_payment_page(transaction_params)

        payment_url = init_response['_links']['next_url']['href']
        payment_id = init_response['payment_id']

        # create pending transaction
        register_transaction(
            self.registration,
            self.registration.price,
            self.registration.currency,
            TransactionAction.pending,
            PROVIDER_GOVUKPAY,
            {'payment_id': payment_id}
        )
        return redirect(payment_url)

class UserSuccessHandler(RHGovukPayBase):
    def _process(self):
        payment_id = self.registration.transaction.data["payment_id"]
        register_transaction(
            self.registration,
            self.registration.price,
            self.registration.currency,
            TransactionAction.complete,
            PROVIDER_GOVUKPAY,
            {'payment_id': payment_id}
        )

        flash(_('Your payment has been confirmed.'), 'success')
        return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))


class UserCancelHandler(RHGovukPayBase):
    """User redirect target in case of cancelled payment."""

    def _process(self):
        payment_id = self.registration.transaction.data["payment_id"]
        register_transaction(
            self.registration,
            self.registration.transaction.amount,
            self.registration.transaction.currency,
            TransactionAction.cancel,
            PROVIDER_GOVUKPAY,
            {'payment_id': payment_id}
        )
        flash(_('You cancelled the payment.'), 'info')
        return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))


class UserFailureHandler(RHGovukPayBase):
    """User redirect target in case of failed payment."""

    def _process(self):
        payment_id = self.registration.transaction.data["payment_id"]
        register_transaction(
            self.registration,
            self.registration.transaction.amount,
            self.registration.transaction.currency,
            TransactionAction.reject,
            PROVIDER_GOVUKPAY,
            {'payment_id': payment_id}
        )
        flash(_('Your payment has failed.'), 'info')
        return redirect(url_for('event_registration.display_regform', self.registration.locator.registrant))
