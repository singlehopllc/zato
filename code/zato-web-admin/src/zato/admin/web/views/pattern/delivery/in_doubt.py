# -*- coding: utf-8 -*-

"""
Copyright (C) 2013 Dariusz Suchojad <dsuch at zato.io>

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
import logging
from json import dumps
from traceback import format_exc

# Django
from django.http import HttpResponse, HttpResponseServerError

# Zato
from zato.admin.web import from_utc_to_user, from_user_to_utc, TARGET_TYPE_HUMAN
from zato.admin.web.forms.pattern.delivery.definition import CreateForm, DeliveryTargetForm, EditForm, InstanceListForm
from zato.admin.web.views import CreateEdit, Delete as _Delete, Index as _Index, get_js_dt_format, method_allowed
from zato.common import DELIVERY_STATE
from zato.common.model import DeliveryItem

logger = logging.getLogger(__name__)

class _CreateEdit(CreateEdit):
    method_allowed = 'POST'

    class SimpleIO(CreateEdit.SimpleIO):
        input_required = ['name', 'target', 'target_type', 'expire_after',
            'expire_arch_succ_after', 'expire_arch_fail_after', 'check_after', 
            'retry_repeats', 'retry_seconds']
        output_required = []

class Index(_Index):
    method_allowed = 'GET'
    url_name = 'pattern-delivery-in-doubt-index'
    template = 'zato/pattern/delivery/in-doubt/index.html'
    service_name = 'zato.pattern.delivery.in-doubt.get-list'
    output_class = DeliveryItem
    
    class SimpleIO(_Index.SimpleIO):
        input_required = ('def_name',)
        input_optional = ('batch_size', 'current_batch', 'start', 'stop',)
        output_required = ('def_name', 'target_type', 'task_id', 'creation_time_utc', 'in_doubt_created_at_utc', 
            'source_count', 'target_count', 'retry_repeats', 'check_after', 'retry_seconds')
        output_repeated = True
        
    def on_before_append_item(self, item):
        item.creation_time = from_utc_to_user(item.creation_time_utc + '+00:00', self.req.zato.user_profile)
        item.in_doubt_created_at = from_utc_to_user(item.in_doubt_created_at_utc + '+00:00', self.req.zato.user_profile)
        return item
    
    def on_after_set_input(self):
        for name in('start', 'stop'):
            if self.input.get(name):
                self.input[name] = from_user_to_utc(self.input[name], self.req.zato.user_profile)
        
    def handle(self):
        out = {'form': InstanceListForm(initial=self.req.GET)}
        out.update(get_js_dt_format(self.req.zato.user_profile))

        service = 'zato.pattern.delivery.get-batch-info'
        req = {key:self.input[key] for key in ('def_name', 'batch_size', 'current_batch', 'start', 'stop') if self.input.get(key)}
        req['state'] = DELIVERY_STATE.IN_DOUBT
        response = self.req.zato.client.invoke(service, req)

        if response.ok:
            out.update(response.data)
        else:
            logger.warn(response.details)
        
        return out

class InDoubtDetails(_Index):
    url_name = 'pattern-delivery-details-in-doubt'
    service_name = 'zato.pattern.delivery.in-doubt.get-details'

class Resubmit(_CreateEdit):
    url_name = 'pattern-delivery-details-in-doubt-resubmit'
    service_name = 'zato.pattern.delivery.resubmit'
    async_invoke = True
    
    class SimpleIO(_CreateEdit.SimpleIO):
        input_required = ('task_id',)
        
    def success_message(self, item):
        return 'Request to resubmit task [{}] sent successfully, check server logs for details'.format(self.input['task_id'])

def _update_many(req, cluster_id, service, success_msg, failure_msg):
    """ A common function for either resubmitting or deleting one or more tasks.
    """
    try:
        for task_id in req.POST.values():
            input_dict = {'task_id':task_id}
            response = req.zato.client.invoke_async(service, input_dict)
            
            if not response.ok:
                raise Exception(response.details)
        
        return HttpResponse(dumps({'message':success_msg}))

    except Exception, e:
        msg = '{}, e:[{}]'.format(failure_msg, format_exc(e))
        logger.error(msg)
        return HttpResponseServerError(msg)
    
@method_allowed('POST')
def resubmit_many(req, cluster_id):
    """ Resubmits one or more delivery tasks.
    """
    return _update_many(req, cluster_id, 'zato.pattern.delivery.resubmit',
        'Request sent successfully, check server logs for details', 'Could not resubmit tasks')

@method_allowed('POST')
def delete_many(req, cluster_id):
    """ Resubmits one or more delivery tasks.
    """
    return _update_many(req, cluster_id, 'zato.pattern.delivery.delete',
        'Tasks deleted successfully', 'Could not delete tasks')
    
# ##############################################################################
