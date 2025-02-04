# -*- coding: utf-8 -*-

"""
Copyright (C) 2011 Dariusz Suchojad <dsuch at zato.io>

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
import logging, inspect, os, sys
from copy import deepcopy
from errno import ENOENT
from json import loads
from threading import RLock
from time import sleep
from traceback import format_exc
from urlparse import urlparse
from uuid import uuid4

# Bunch
from bunch import Bunch

# dateutil
from dateutil.parser import parse
from dateutil.relativedelta import relativedelta
from dateutil.rrule import DAILY, MINUTELY, rrule

# gunicorn
from gunicorn.workers.ggevent import GeventWorker as GunicornGeventWorker
from gunicorn.workers.sync import SyncWorker as GunicornSyncWorker

# Zato
from zato.common import CHANNEL, DATA_FORMAT, HTTP_SOAP_SERIALIZATION_TYPE, MSG_PATTERN_TYPE, PUB_SUB, SEC_DEF_TYPE, SIMPLE_IO, \
     TRACE1, ZATO_ODB_POOL_NAME
from zato.common import broker_message
from zato.common.broker_message import code_to_name
from zato.common.dispatch import dispatcher
from zato.common.pubsub import Client, Consumer, Topic
from zato.common.util import new_cid, pairwise, parse_extra_into_dict, get_validate_tls_key_cert
from zato.server.base import BrokerMessageReceiver
from zato.server.connection.cassandra import CassandraAPI, CassandraConnStore
from zato.server.connection.cloud.aws.s3 import S3Wrapper
from zato.server.connection.cloud.openstack.swift import SwiftWrapper
from zato.server.connection.email import IMAPAPI, IMAPConnStore, SMTPAPI, SMTPConnStore
from zato.server.connection.ftp import FTPStore
from zato.server.connection.http_soap.channel import RequestDispatcher, RequestHandler
from zato.server.connection.http_soap.outgoing import HTTPSOAPWrapper, SudsSOAPWrapper
from zato.server.connection.http_soap.url_data import URLData
from zato.server.connection.search.es import ElasticSearchAPI, ElasticSearchConnStore
from zato.server.connection.search.solr import SolrAPI, SolrConnStore
from zato.server.connection.sql import PoolStore, SessionWrapper
from zato.server.message import JSONPointerStore, NamespaceStore, XPathStore
from zato.server.query import CassandraQueryAPI, CassandraQueryStore
from zato.server.stats import MaintenanceTool

logger = logging.getLogger(__name__)

class GeventWorker(GunicornGeventWorker):
    def __init__(self, *args, **kwargs):
        self.deployment_key = uuid4().hex
        super(GunicornGeventWorker, self).__init__(*args, **kwargs)

class SyncWorker(GunicornSyncWorker):
    def __init__(self, *args, **kwargs):
        self.deployment_key = uuid4().hex
        super(GunicornSyncWorker, self).__init__(*args, **kwargs)

class WorkerStore(BrokerMessageReceiver):
    """ Each worker thread has its own configuration store. The store is assigned
    to the thread's threading.local variable. All the methods assume the data's
    being already validated and sanitized by one of Zato's internal services.

    There are exactly two threads willing to access the data at any time
    - the worker thread this store belongs to
    - the background ZeroMQ thread which may wish to update the store's configuration
    hence the need for employing RLocks yet there shouldn't be much contention
    because configuration updates are extremaly rare when compared to regular
    access by worker threads.
    """
    def __init__(self, worker_config=None, server=None):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.is_ready = False
        self.worker_config = worker_config
        self.server = server
        self.update_lock = RLock()
        self.kvdb = server.kvdb
        self.broker_client = None

        self.pubsub = None
        """:type: zato.common.pubsub.PubSubAPI"""

    def init(self):

        # Statistics maintenance
        self.stats_maint = MaintenanceTool(self.kvdb.conn)

        self.msg_ns_store = NamespaceStore()
        self.json_pointer_store = JSONPointerStore()
        self.xpath_store = XPathStore()

        # Cassandra
        self.cassandra_api = CassandraAPI(CassandraConnStore())
        self.cassandra_query_store = CassandraQueryStore()
        self.cassandra_query_api = CassandraQueryAPI(self.cassandra_query_store)

        # Search
        self.search_es_api = ElasticSearchAPI(ElasticSearchConnStore())
        self.search_solr_api = SolrAPI(SolrConnStore())

        # E-mail
        self.email_smtp_api = SMTPAPI(SMTPConnStore())
        self.email_imap_api = IMAPAPI(IMAPConnStore())

        # Message-related config - init_msg_ns_store must come before init_xpath_store
        # so the latter has access to the former's namespace map.
        self.init_msg_ns_store()
        self.init_json_pointer_store()
        self.init_xpath_store()

        self.init_cassandra()
        self.init_cassandra_queries()

        self.init_search_es()
        self.init_search_solr()

        self.init_email_smtp()
        self.init_email_imap()

        # Request dispatcher - matches URLs, checks security and dispatches HTTP
        # requests to services.
        self.request_dispatcher = RequestDispatcher(simple_io_config=self.worker_config.simple_io)
        self.request_dispatcher.url_data = URLData(
            deepcopy(self.worker_config.http_soap),
            self.server.odb.get_url_security(self.server.cluster_id, 'channel')[0],
            self.worker_config.basic_auth, self.worker_config.ntlm, self.worker_config.oauth, self.worker_config.tech_acc,
            self.worker_config.wss, self.worker_config.apikey, self.worker_config.aws, self.worker_config.openstack_security,
            self.worker_config.xpath_sec, self.worker_config.tls_key_cert, self.kvdb, self.broker_client, self.server.odb,
            self.json_pointer_store, self.xpath_store)

        self.request_dispatcher.request_handler = RequestHandler(self.server)

        # Create all the expected connections and objects
        self.init_sql()
        self.init_ftp()
        self.init_http_soap()
        self.init_cloud()
        self.init_pubsub()
        self.init_notifiers()

        # All set, whoever is waiting for us, if anyone at all, can now proceed
        self.is_ready = True

    def filter(self, msg):
        # TODO: Fix it, worker doesn't need to accept all the messages
        return True

    def _update_queue_build_cap(self, item):
        item.queue_build_cap = float(self.server.fs_server_config.misc.queue_build_cap)

    def _update_aws_config(self, msg):
        """ Parses the address to AWS we store into discrete components S3Connection objects expect.
        Also turns metadata string into a dictionary
        """
        url_info = urlparse(msg.address)

        msg.is_secure = True if url_info.scheme == 'https' else False
        msg.port = url_info.port if url_info.port else (443 if msg.is_secure else 80)
        msg.host = url_info.netloc

        msg.metadata = parse_extra_into_dict(msg.metadata_)

    def _http_soap_wrapper_from_config(self, config, has_sec_config=True):
        """ Creates a new HTTP/SOAP connection wrapper out of a configuration
        dictionary.
        """
        security_name = config.get('security_name')
        sec_config = {'security_name':security_name, 'sec_type':None, 'username':None, 'password':None, 'password_type':None}
        _sec_config = None

        # This will be set to True only if the method's invoked on a server's starting up
        if has_sec_config:
            # It's possible that there is no security config attached at all
            if security_name:
                _sec_config = config
        else:
            if security_name:
                sec_type = config.sec_type
                func = getattr(self.request_dispatcher.url_data, sec_type + '_get')
                _sec_config = func(security_name).config

        if logger.isEnabledFor(TRACE1):
            logger.log(TRACE1, 'has_sec_config:[{}], security_name:[{}], _sec_config:[{}]'.format(
                has_sec_config, security_name, _sec_config))

        if _sec_config:
            sec_config['sec_type'] = _sec_config['sec_type']
            sec_config['username'] = _sec_config.get('username')
            sec_config['password'] = _sec_config.get('password')
            sec_config['password_type'] = _sec_config.get('password_type')
            sec_config['salt'] = _sec_config.get('salt')

            if sec_config['sec_type'] == SEC_DEF_TYPE.TLS_KEY_CERT:
                tls = self.request_dispatcher.url_data.tls_key_cert_get(security_name)
                _, _, full_path = get_validate_tls_key_cert(self.server.tls_dir, tls.config.fs_name)
                sec_config['tls_key_cert_full_path'] = full_path

        wrapper_config = {'id':config.id,
            'is_active':config.is_active, 'method':config.method,
            'data_format':config.get('data_format'),
            'name':config.name, 'transport':config.transport,
            'address_host':config.host,
            'address_url_path':config.url_path,
            'soap_action':config.soap_action, 'soap_version':config.soap_version, 'ping_method':config.ping_method,
            'pool_size':config.pool_size, 'serialization_type':config.serialization_type,
            'timeout':config.timeout}
        wrapper_config.update(sec_config)

        if wrapper_config['serialization_type'] == HTTP_SOAP_SERIALIZATION_TYPE.SUDS.id:
            wrapper_config['queue_build_cap'] = float(self.server.fs_server_config.misc.queue_build_cap)
            wrapper = SudsSOAPWrapper(wrapper_config)
            wrapper.build_client_queue()
            return wrapper

        return HTTPSOAPWrapper(wrapper_config)

# ################################################################################################################################

    def init_sql(self):
        """ Initializes SQL connections, first to ODB and then any user-defined ones.
        """
        # We need a store first
        self.sql_pool_store = PoolStore()

        # Connect to ODB
        self.sql_pool_store[ZATO_ODB_POOL_NAME] = self.worker_config.odb_data
        self.odb = SessionWrapper()
        self.odb.init_session(ZATO_ODB_POOL_NAME, self.worker_config.odb_data, self.sql_pool_store[ZATO_ODB_POOL_NAME].pool)

        # Any user-defined SQL connections left?
        for pool_name in self.worker_config.out_sql:
            config = self.worker_config.out_sql[pool_name]['config']
            self.sql_pool_store[pool_name] = config

    def init_ftp(self):
        """ Initializes FTP connetions. The method replaces whatever value self.out_ftp
        previously had (initially this would be a ConfigDict of connection definitions).
        """
        config_list = self.worker_config.out_ftp.get_config_list()
        self.worker_config.out_ftp = FTPStore()
        self.worker_config.out_ftp.add_params(config_list)

    def init_http_soap(self):
        """ Initializes plain HTTP/SOAP connections.
        """
        for transport in('soap', 'plain_http'):
            config_dict = getattr(self.worker_config, 'out_' + transport)
            for name in config_dict:
                config = config_dict[name].config

                wrapper = self._http_soap_wrapper_from_config(config)
                config_dict[name].conn = wrapper

                # To make the API consistent with that of SQL connection pools
                config_dict[name].ping = wrapper.ping

    def init_cloud(self):
        """ Initializes all the cloud connections.
        """
        data = (
            ('cloud_openstack_swift', SwiftWrapper),
            ('cloud_aws_s3', S3Wrapper),
        )

        for config_key, wrapper in data:
            config_attr = getattr(self.worker_config, config_key)
            for name in config_attr:
                config = config_attr[name]['config']
                if isinstance(wrapper, S3Wrapper):
                    self._update_aws_config(config)
                config.queue_build_cap = float(self.server.fs_server_config.misc.queue_build_cap)
                config_attr[name].conn = wrapper(config)
                config_attr[name].conn.build_queue()

    def _update_cloud_openstack_swift_container(self, config_dict):
        """ Makes sure OpenStack Swift containers always have a path to prefix queries with.
        """
        config_dict.containers = [elem.split(':') for elem in config_dict.containers.splitlines()]
        for item in config_dict.containers:
            # No path specified so we use an empty string to catch everything.
            if len(item) == 1:
                item.append('')

            item.append('{}:{}'.format(item[0], item[1]))

    def init_notifiers(self):
        for config_dict in self.worker_config.notif_cloud_openstack_swift.values():
            self._update_cloud_openstack_swift_container(config_dict.config)

# ################################################################################################################################

    def init_cassandra(self):
        for k, v in self.worker_config.cassandra_conn.items():
            try:
                self.update_cassandra_conn(v.config)
                self.cassandra_api.create_def(k, v.config)
            except Exception, e:
                logger.warn('Could not create a Cassandra connection `%s`, e:`%s`', k, format_exc(e))

# ################################################################################################################################

    def init_cassandra_queries(self):
        for k, v in self.worker_config.cassandra_query.items():
            try:
                self.cassandra_query_api.create(k, v.config, def_=self.cassandra_api[v.config.def_name])
            except Exception, e:
                logger.warn('Could not create a Cassandra query `%s`, e:`%s`', k, format_exc(e))

# ################################################################################################################################

    def init_simple(self, config, api, name):
        for k, v in config.items():
            self._update_queue_build_cap(v.config)
            try:
                api.create(k, v.config)
            except Exception, e:
                logger.warn('Could not create {} connection `%s`, e:`%s`'.format(name), k, format_exc(e))

# ################################################################################################################################

    def init_search_es(self):
        self.init_simple(self.worker_config.search_es, self.search_es_api, 'an ElasticSearch')

# ################################################################################################################################

    def init_search_solr(self):
        self.init_simple(self.worker_config.search_solr, self.search_solr_api, 'a Solr')

# ################################################################################################################################

    def init_email_smtp(self):
        self.init_simple(self.worker_config.email_smtp, self.email_smtp_api, 'an SMTP')

# ################################################################################################################################

    def init_email_imap(self):
        self.init_simple(self.worker_config.email_imap, self.email_imap_api, 'an IMAP')

# ################################################################################################################################

    def _topic_from_topic_data(self, data):
        return Topic(data.name, data.is_active, True, data.max_depth)

    def _add_pubsub_topic(self, data):
        self.pubsub.add_topic(self._topic_from_topic_data(data))

    def init_pubsub(self):
        """ Initializes publish/subscribe mechanisms.
        """
        self.pubsub.set_default_consumer(self.worker_config.pubsub.default_consumer)
        self.pubsub.set_default_producer(self.worker_config.pubsub.default_producer)

        for topic_name, topic_data in self.worker_config.pubsub.topics.items():
            self._add_pubsub_topic(topic_data.config)

        for list_value in self.worker_config.pubsub.producers.values():
            for config in list_value:
                self.pubsub.add_producer(Client(config.client_id, config.name, config.is_active), Topic(config.topic_name))

        for list_value in self.worker_config.pubsub.consumers.values():
            for config in list_value:

                callback_type = PUB_SUB.CALLBACK_TYPE.OUTCONN_SOAP if bool(config.soap_version) else \
                    PUB_SUB.CALLBACK_TYPE.OUTCONN_PLAIN_HTTP

                self.pubsub.add_consumer(
                    Consumer(
                        config.client_id, config.name, config.is_active, config.sub_key, config.max_backlog,
                        config.delivery_mode, config.callback_id, config.callback_name, callback_type),
                    Topic(config.topic_name))

# ################################################################################################################################

    def init_msg_ns_store(self):
        for k, v in self.worker_config.msg_ns.items():
            self.msg_ns_store.add(k, v.config)

    def init_xpath_store(self):
        for k, v in self.worker_config.xpath.items():
            self.xpath_store.add(k, v.config, self.msg_ns_store.ns_map)

    def init_json_pointer_store(self):
        for k, v in self.worker_config.json_pointer.items():
            self.json_pointer_store.add(k, v.config.value)

# ################################################################################################################################

    def _update_auth(self, msg, action_name, sec_type, visit_wrapper, keys=None):
        """ A common method for updating auth-related configuration.
        """
        with self.update_lock:
            # Channels
            handler = getattr(self.request_dispatcher.url_data, 'on_broker_msg_' + action_name)
            handler(msg)

            for transport in('soap', 'plain_http'):
                config_dict = getattr(self.worker_config, 'out_' + transport)

                # Wrappers and static configuration for outgoing connections
                for name in config_dict.copy_keys():
                    config = config_dict[name].config
                    wrapper = config_dict[name].conn
                    if config['sec_type'] == sec_type:
                        if keys:
                            visit_wrapper(wrapper, msg, keys)
                        else:
                            visit_wrapper(wrapper, msg)

    def _visit_wrapper_edit(self, wrapper, msg, keys):
        """ Updates a given wrapper's security configuration.
        """
        if wrapper.config['security_name'] == msg['old_name']:
            for key in keys:
                # All's good except for 'name', the msg's 'name' is known
                # as 'security_name' in wrapper's config.
                if key == 'name':
                    key1 = 'security_name'
                    key2 = key
                else:
                    key1, key2 = key, key
                wrapper.config[key1] = msg[key2]
            wrapper.set_auth()

    def _visit_wrapper_delete(self, wrapper, msg):
        """ Deletes a wrapper.
        """
        config_dict = getattr(self.worker_config, 'out_' + wrapper.config['transport'])
        if wrapper.config['security_name'] == msg['name']:
            del config_dict[wrapper.config['name']]

    def _visit_wrapper_change_password(self, wrapper, msg):
        """ Changes a wrapper's password.
        """
        if wrapper.config['security_name'] == msg['name']:
            wrapper.config['password'] = msg['password']
            wrapper.set_auth()

# ################################################################################################################################

    def apikey_get(self, name):
        """ Returns the configuration of the API key of the given name.
        """
        return self.request_dispatcher.url_data.apikey_get(name)

    def on_broker_msg_SECURITY_APIKEY_CREATE(self, msg, *args):
        """ Creates a new API key security definition.
        """
        dispatcher.notify(broker_message.SECURITY.APIKEY_CREATE.value, msg)

    def on_broker_msg_SECURITY_APIKEY_EDIT(self, msg, *args):
        """ Updates an existing API key security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.APIKEY,
                self._visit_wrapper_edit, keys=('is_active', 'username', 'name'))

    def on_broker_msg_SECURITY_APIKEY_DELETE(self, msg, *args):
        """ Deletes an API key security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.APIKEY,
                self._visit_wrapper_delete)

    def on_broker_msg_SECURITY_APIKEY_CHANGE_PASSWORD(self, msg, *args):
        """ Changes password of an API key security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.APIKEY,
                self._visit_wrapper_change_password)

# ################################################################################################################################

    def aws_get(self, name):
        """ Returns the configuration of the AWS security definition
        of the given name.
        """
        return self.request_dispatcher.url_data.aws_get(name)

    def on_broker_msg_SECURITY_AWS_CREATE(self, msg, *args):
        """ Creates a new AWS security definition
        """
        dispatcher.notify(broker_message.SECURITY.AWS_CREATE.value, msg)

    def on_broker_msg_SECURITY_AWS_EDIT(self, msg, *args):
        """ Updates an existing AWS security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.AWS,
                self._visit_wrapper_edit, keys=('is_active', 'username', 'name'))

    def on_broker_msg_SECURITY_AWS_DELETE(self, msg, *args):
        """ Deletes an AWS security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.AWS,
                self._visit_wrapper_delete)

    def on_broker_msg_SECURITY_AWS_CHANGE_PASSWORD(self, msg, *args):
        """ Changes password of an AWS security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.AWS,
                self._visit_wrapper_change_password)

# ################################################################################################################################

    def openstack_get(self, name):
        """ Returns the configuration of the OpenStack security definition
        of the given name.
        """
        self.request_dispatcher.url_data.openstack_get(name)

    def on_broker_msg_SECURITY_OPENSTACK_CREATE(self, msg, *args):
        """ Creates a new OpenStack security definition
        """
        dispatcher.notify(broker_message.SECURITY.OPENSTACK_CREATE.value, msg)

    def on_broker_msg_SECURITY_OPENSTACK_EDIT(self, msg, *args):
        """ Updates an existing OpenStack security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.OPENSTACK,
                self._visit_wrapper_edit, keys=('is_active', 'username', 'name'))

    def on_broker_msg_SECURITY_OPENSTACK_DELETE(self, msg, *args):
        """ Deletes an OpenStack security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.OPENSTACK,
                self._visit_wrapper_delete)

    def on_broker_msg_SECURITY_OPENSTACK_CHANGE_PASSWORD(self, msg, *args):
        """ Changes password of an OpenStack security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.OPENSTACK,
                self._visit_wrapper_change_password)

# ################################################################################################################################

    def ntlm_get(self, name):
        """ Returns the configuration of the NTLM security definition
        of the given name.
        """
        return self.request_dispatcher.url_data.ntlm_get(name)

    def on_broker_msg_SECURITY_NTLM_CREATE(self, msg, *args):
        """ Creates a new NTLM security definition
        """
        dispatcher.notify(broker_message.SECURITY.NTLM_CREATE.value, msg)

    def on_broker_msg_SECURITY_NTLM_EDIT(self, msg, *args):
        """ Updates an existing NTLM security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.NTLM,
                self._visit_wrapper_edit, keys=('is_active', 'username', 'name'))

    def on_broker_msg_SECURITY_NTLM_DELETE(self, msg, *args):
        """ Deletes an NTLM security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.NTLM,
                self._visit_wrapper_delete)

    def on_broker_msg_SECURITY_NTLM_CHANGE_PASSWORD(self, msg, *args):
        """ Changes password of an NTLM security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.NTLM,
                self._visit_wrapper_change_password)

# ################################################################################################################################

    def basic_auth_get(self, name):
        """ Returns the configuration of the HTTP Basic Auth security definition
        of the given name.
        """
        return self.request_dispatcher.url_data.basic_auth_get(name)

    def on_broker_msg_SECURITY_BASIC_AUTH_CREATE(self, msg, *args):
        """ Creates a new HTTP Basic Auth security definition
        """
        dispatcher.notify(broker_message.SECURITY.BASIC_AUTH_CREATE.value, msg)

    def on_broker_msg_SECURITY_BASIC_AUTH_EDIT(self, msg, *args):
        """ Updates an existing HTTP Basic Auth security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.BASIC_AUTH,
                self._visit_wrapper_edit, keys=('is_active', 'username', 'name'))

    def on_broker_msg_SECURITY_BASIC_AUTH_DELETE(self, msg, *args):
        """ Deletes an HTTP Basic Auth security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.BASIC_AUTH,
                self._visit_wrapper_delete)

    def on_broker_msg_SECURITY_BASIC_AUTH_CHANGE_PASSWORD(self, msg, *args):
        """ Changes password of an HTTP Basic Auth security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.BASIC_AUTH,
                self._visit_wrapper_change_password)

# ################################################################################################################################

    def oauth_get(self, name):
        """ Returns the configuration of the OAuth security definition
        of the given name.
        """
        return self.request_dispatcher.url_data.oauth_get(name)

    def on_broker_msg_SECURITY_OAUTH_CREATE(self, msg, *args):
        """ Creates a new OAuth security definition
        """
        dispatcher.notify(broker_message.SECURITY.OAUTH_CREATE.value, msg)

    def on_broker_msg_SECURITY_OAUTH_EDIT(self, msg, *args):
        """ Updates an existing OAuth security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.OAUTH,
                self._visit_wrapper_edit, keys=('is_active', 'username', 'name'))

    def on_broker_msg_SECURITY_OAUTH_DELETE(self, msg, *args):
        """ Deletes an OAuth security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.OAUTH,
                self._visit_wrapper_delete)

    def on_broker_msg_SECURITY_OAUTH_CHANGE_PASSWORD(self, msg, *args):
        """ Changes password of an OAuth security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.OAUTH,
                self._visit_wrapper_change_password)

# ################################################################################################################################

    def tech_acc_get(self, name):
        """ Returns the configuration of the technical account of the given name.
        """
        self.request_dispatcher.url_data.tech_acc_get(name)

    def on_broker_msg_SECURITY_TECH_ACC_CREATE(self, msg, *args):
        """ Creates a new technical account.
        """
        dispatcher.notify(broker_message.SECURITY.TECH_ACC_CREATE.value, msg)

    def on_broker_msg_SECURITY_TECH_ACC_EDIT(self, msg, *args):
        """ Updates an existing technical account.
        """
        dispatcher.notify(broker_message.SECURITY.TECH_ACC_EDIT.value, msg)

    def on_broker_msg_SECURITY_TECH_ACC_DELETE(self, msg, *args):
        """ Deletes a technical account.
        """
        dispatcher.notify(broker_message.SECURITY.TECH_ACC_DELETE.value, msg)

    def on_broker_msg_SECURITY_TECH_ACC_CHANGE_PASSWORD(self, msg, *args):
        """ Changes the password of a technical account.
        """
        dispatcher.notify(broker_message.SECURITY.TECH_ACC_CHANGE_PASSWORD.value, msg)

# ################################################################################################################################

    def update_tls_key_cert(self, msg):
        _, _, full_path = get_validate_tls_key_cert(self.server.tls_dir, msg.fs_name)
        msg.full_path = full_path

    def on_broker_msg_SECURITY_TLS_KEY_CERT_CREATE(self, msg):
        self.update_tls_key_cert(msg)
        dispatcher.notify(broker_message.SECURITY.TLS_KEY_CERT_CREATE.value, msg)

    def on_broker_msg_SECURITY_TLS_KEY_CERT_EDIT(self, msg):
        self.update_tls_key_cert(msg)
        dispatcher.notify(broker_message.SECURITY.TLS_KEY_CERT_EDIT.value, msg)

    def on_broker_msg_SECURITY_TLS_KEY_CERT_DELETE(self, msg):
        self.update_tls_key_cert(msg)
        dispatcher.notify(broker_message.SECURITY.TLS_KEY_CERT_DELETE.value, msg)

# ################################################################################################################################

    def wss_get(self, name):
        """ Returns the configuration of the WSS definition of the given name.
        """
        self.request_dispatcher.url_data.wss_get(name)

    def on_broker_msg_SECURITY_WSS_CREATE(self, msg, *args):
        """ Creates a new WS-Security definition.
        """
        dispatcher.notify(broker_message.SECURITY.WSS_CREATE.value, msg)

    def on_broker_msg_SECURITY_WSS_EDIT(self, msg, *args):
        """ Updates an existing WS-Security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.WSS,
                self._visit_wrapper_edit, keys=('is_active', 'username', 'name',
                    'nonce_freshness_time', 'reject_expiry_limit', 'password_type',
                    'reject_empty_nonce_creat', 'reject_stale_tokens'))

    def on_broker_msg_SECURITY_WSS_DELETE(self, msg, *args):
        """ Deletes a WS-Security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.WSS,
                self._visit_wrapper_delete)

    def on_broker_msg_SECURITY_WSS_CHANGE_PASSWORD(self, msg, *args):
        """ Changes the password of a WS-Security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.WSS,
                self._visit_wrapper_change_password)

# ################################################################################################################################

    def xpath_sec_get(self, name):
        """ Returns the configuration of an XPath security definition of the given name.
        """
        self.request_dispatcher.url_data.xpath_sec_get(name)

    def on_broker_msg_SECURITY_XPATH_SEC_CREATE(self, msg, *args):
        """ Creates a new XPath security definition
        """
        dispatcher.notify(broker_message.SECURITY.XPATH_SEC_CREATE.value, msg)

    def on_broker_msg_SECURITY_XPATH_SEC_EDIT(self, msg, *args):
        """ Updates an existing XPath security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.XPATH_SEC,
                self._visit_wrapper_edit, keys=('is_active', 'username', 'name'))

    def on_broker_msg_SECURITY_XPATH_SEC_DELETE(self, msg, *args):
        """ Deletes an XPath security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.XPATH_SEC,
                self._visit_wrapper_delete)

    def on_broker_msg_SECURITY_XPATH_SEC_CHANGE_PASSWORD(self, msg, *args):
        """ Changes password of an XPath security definition.
        """
        self._update_auth(msg, code_to_name[msg.action], SEC_DEF_TYPE.XPATH_SEC,
                self._visit_wrapper_change_password)

# ################################################################################################################################

    def _set_service_response_data(self, service, **ignored):
        if not isinstance(service.response.payload, basestring):
            service.response.payload = service.response.payload.getvalue()

    def _on_message_invoke_service(self, msg, channel, action, args=None):
        """ Triggered by external processes, such as AMQP or the singleton's scheduler,
        creates a new service instance and invokes it.
        """
        # WSGI environment is the best place we have to store raw msg in
        wsgi_environ = {'zato.request_ctx.async_msg':msg}

        service = self.server.service_store.new_instance_by_name(msg['service'])
        service.update_handle(self._set_service_response_data, service, msg['payload'],
            channel, msg.get('data_format'), msg.get('transport'), self.server,
            self.broker_client, self, msg['cid'], self.worker_config.simple_io,
            job_type=msg.get('job_type'), wsgi_environ=wsgi_environ)

# ################################################################################################################################

    def on_broker_msg_SCHEDULER_JOB_EXECUTED(self, msg, args=None):
        return self._on_message_invoke_service(msg, CHANNEL.SCHEDULER, 'SCHEDULER_JOB_EXECUTED', args)

    def on_broker_msg_CHANNEL_AMQP_MESSAGE_RECEIVED(self, msg, args=None):
        return self._on_message_invoke_service(msg, CHANNEL.AMQP, 'CHANNEL_AMQP_MESSAGE_RECEIVED', args)

    def on_broker_msg_CHANNEL_JMS_WMQ_MESSAGE_RECEIVED(self, msg, args=None):
        return self._on_message_invoke_service(msg, CHANNEL.JMS_WMQ, 'CHANNEL_JMS_WMQ_MESSAGE_RECEIVED', args)

    def on_broker_msg_CHANNEL_ZMQ_MESSAGE_RECEIVED(self, msg, args=None):
        return self._on_message_invoke_service(msg, CHANNEL.ZMQ, 'CHANNEL_ZMQ_MESSAGE_RECEIVED', args)

# ################################################################################################################################

    def on_broker_msg_OUTGOING_SQL_CREATE_EDIT(self, msg, *args):
        """ Creates or updates an SQL connection, including changing its
        password.
        """
        # Is it a rename? If so, delete the connection first
        if msg.get('old_name') and msg.get('old_name') != msg['name']:
            del self.sql_pool_store[msg['old_name']]

        self.sql_pool_store[msg['name']] = msg

    def on_broker_msg_OUTGOING_SQL_CHANGE_PASSWORD(self, msg, *args):
        """ Deletes an outgoing SQL connection pool and recreates it using the
        new password.
        """
        self.sql_pool_store.change_password(msg['name'], msg['password'])

    def on_broker_msg_OUTGOING_SQL_DELETE(self, msg, *args):
        """ Deletes an outgoing SQL connection pool.
        """
        del self.sql_pool_store[msg['name']]

# ################################################################################################################################

    def get_channel_plain_http(self, name):
        with self.update_lock:
            for item in self.request_dispatcher.url_data.channel_data:
                if item.connection == 'channel' and item.name == name:
                    return item

    def on_broker_msg_CHANNEL_HTTP_SOAP_CREATE_EDIT(self, msg, *args):
        """ Creates or updates an HTTP/SOAP channel.
        """
        self.request_dispatcher.url_data.on_broker_msg_CHANNEL_HTTP_SOAP_CREATE_EDIT(msg, *args)

    def on_broker_msg_CHANNEL_HTTP_SOAP_DELETE(self, msg, *args):
        """ Deletes an HTTP/SOAP channel.
        """
        self.request_dispatcher.url_data.on_broker_msg_CHANNEL_HTTP_SOAP_DELETE(msg, *args)

# ################################################################################################################################

    def _delete_config_close_wrapper(self, name, config_dict, conn_type, log_func):
        """ Deletes a wrapper-based connection's config and closes its underlying wrapper.
        """
        # Delete the connection first, if it exists at all ..
        try:
            try:
                wrapper = config_dict[name].conn
            except (KeyError, AttributeError), e:
                log_func('Could not access wrapper, e:[{}]'.format(format_exc(e)))
            else:
                try:
                    wrapper.session.close()
                finally:
                    del config_dict[name]
        except Exception, e:
            log_func('Could not delete `{}`, e:`{}`'.format(conn_type, format_exc(e)))

# ################################################################################################################################

    def _delete_config_close_wrapper_http_soap(self, name, transport, log_func):
        """ Deletes/closes an HTTP/SOAP outconn.
        """ 
        # Are we dealing with plain HTTP or SOAP?
        config_dict = getattr(self.worker_config, 'out_' + transport)

        return self._delete_config_close_wrapper(name, config_dict, 'an outgoing HTTP/SOAP connection', log_func)

    def on_broker_msg_OUTGOING_HTTP_SOAP_CREATE_EDIT(self, msg, *args):
        """ Creates or updates an outgoing HTTP/SOAP connection.
        """
        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg['name']

        # .. delete the connection if it exists ..
        self._delete_config_close_wrapper_http_soap(del_name, msg['transport'], logger.debug)

        # .. and create a new one
        wrapper = self._http_soap_wrapper_from_config(msg, False)
        config_dict = getattr(self.worker_config, 'out_' + msg['transport'])
        config_dict[msg['name']] = Bunch()
        config_dict[msg['name']].config = msg
        config_dict[msg['name']].conn = wrapper
        config_dict[msg['name']].ping = wrapper.ping # (just like in self.init_http)

    def on_broker_msg_OUTGOING_HTTP_SOAP_DELETE(self, msg, *args):
        """ Deletes an outgoing HTTP/SOAP connection (actually delegates the
        task to self._delete_config_close_wrapper_http_soap.
        """
        self._delete_config_close_wrapper_http_soap(msg['name'], msg['transport'], logger.error)

# ################################################################################################################################

    def on_broker_msg_SERVICE_DELETE(self, msg, *args):
        """ Deletes the service from the service store and removes it from the filesystem
        if it's not an internal one.
        """
        # Module this service is in so it can be removed from sys.modules
        mod = inspect.getmodule(self.server.service_store.services[msg.impl_name]['service_class'])

        # Where to delete it from in the second step
        fs_location = self.server.service_store.services[msg.impl_name]['deployment_info']['fs_location']

        # Delete it from the service store
        del self.server.service_store.services[msg.impl_name]

        # Delete it from the filesystem, including any bytecode left over. Note that
        # other parallel servers may wish to do exactly the same so we just ignore
        # the error if any files are missing. Also note that internal services won't
        # be ever deleted from the FS.
        if not msg.is_internal:
            all_ext = ('py', 'pyc', 'pyo')
            no_ext = '.'.join(fs_location.split('.')[:-1])
            for ext in all_ext:
                path = '{}.{}'.format(no_ext, ext)
                try:
                    os.remove(path)
                except OSError, e:
                    if e.errno != ENOENT:
                        raise

        # Makes it actually gets reimported next time it's redeployed
        del sys.modules[mod.__name__]

    def on_broker_msg_SERVICE_EDIT(self, msg, *args):
        for name in('is_active', 'slow_threshold'):
            self.server.service_store.services[msg.impl_name][name] = msg[name]

# ################################################################################################################################

    def on_broker_msg_OUTGOING_FTP_CREATE_EDIT(self, msg, *args):
        self.worker_config.out_ftp.create_edit(msg, msg.get('old_name'))

    def on_broker_msg_OUTGOING_FTP_DELETE(self, msg, *args):
        self.worker_config.out_ftp.delete(msg.name)

    def on_broker_msg_OUTGOING_FTP_CHANGE_PASSWORD(self, msg, *args):
        self.worker_config.out_ftp.change_password(msg.name, msg.password)

# ################################################################################################################################

    def on_broker_msg_HOT_DEPLOY_CREATE(self, msg, *args):
        msg.cid = new_cid()
        msg.service = 'zato.hot-deploy.create'
        msg.payload = {'package_id': msg.package_id}
        msg.data_format = SIMPLE_IO.FORMAT.JSON
        return self._on_message_invoke_service(msg, 'hot-deploy', 'HOT_DEPLOY_CREATE', args)

# ################################################################################################################################

    def on_broker_msg_STATS_DELETE(self, msg, *args):
        start = parse(msg.start)
        stop = parse(msg.stop)

        # Looks weird but this is so we don't have to create a list instead of a generator
        # (and Python 3 won't leak the last element anymore)
        last_elem = None

        # Are the dates are at least a day apart? If so, we'll split the interval
        # into smaller one day-long batches.
        if(stop-start).days:
            for elem1, elem2 in pairwise(elem for elem in rrule(DAILY, dtstart=start, until=stop)):
                self.broker_client.invoke_async(
                    {'action':broker_message.STATS.DELETE_DAY.value, 'start':elem1.isoformat(), 'stop':elem2.isoformat()})

                # So as not to drown the broker with a sudden surge of messages
                sleep(0.02)

                last_elem = elem2

            # It's possible we still have something left over. Let's say
            #
            # start = '2012-07-24T02:02:53'
            # stop = '2012-07-25T02:04:53'
            #
            # The call to rrule(DAILY, ...) will nicely slice the time between
            # start and stop into one day intervals yet the last element of the slice
            # will have the time portion equal to that of start - so in this
            # particular case it would be that last_elem was 2012-07-25T02:02:53
            # which would be still be 2 minutes short of stop. Hence the need for
            # a relativedelta, to tease out the remaining time information.
            delta = relativedelta(stop, last_elem)
            if delta.minutes:
                self.stats_maint.delete(last_elem, stop, MINUTELY)

        # Not a full day apart so we can delete everything ourselves
        else:
            self.stats_maint.delete(start, stop, MINUTELY)

    def on_broker_msg_STATS_DELETE_DAY(self, msg, *args):
        self.stats_maint.delete(parse(msg.start), parse(msg.stop), MINUTELY)

# ################################################################################################################################

    def on_broker_msg_SERVICE_PUBLISH(self, msg, args=None):
        return self._on_message_invoke_service(msg, CHANNEL.INVOKE_ASYNC, 'SERVICE_PUBLISH', args)

# ################################################################################################################################

    def on_broker_msg_MSG_NS_CREATE(self, msg, *args):
        """ Creates a new namespace.
        """
        self.msg_ns_store.on_broker_msg_MSG_NS_CREATE(msg, *args)

    def on_broker_msg_MSG_NS_EDIT(self, msg, *args):
        """ Updates an existing namespace.
        """
        self.msg_ns_store.on_broker_msg_MSG_NS_EDIT(msg, *args)

    def on_broker_msg_MSG_NS_DELETE(self, msg, *args):
        """ Deletes a namespace.
        """
        self.msg_ns_store.on_broker_msg_MSG_NS_DELETE(msg, *args)

# ################################################################################################################################

    def on_broker_msg_MSG_XPATH_CREATE(self, msg, *args):
        """ Creates a new XPath.
        """
        self.xpath_store.on_broker_msg_create(msg, self.msg_ns_store.ns_map)

    def on_broker_msg_MSG_XPATH_EDIT(self, msg, *args):
        """ Updates an existing XPath.
        """
        self.xpath_store.on_broker_msg_edit(msg, self.msg_ns_store.ns_map)

    def on_broker_msg_MSG_XPATH_DELETE(self, msg, *args):
        """ Deletes an XPath.
        """
        self.xpath_store.on_broker_msg_delete(msg, *args)

# ################################################################################################################################

    def on_broker_msg_MSG_JSON_POINTER_CREATE(self, msg, *args):
        """ Creates a new JSON Pointer.
        """
        self.json_pointer_store.on_broker_msg_create(msg)

    def on_broker_msg_MSG_JSON_POINTER_EDIT(self, msg, *args):
        """ Updates an existing JSON Pointer.
        """
        self.request_dispatcher.url_data.on_broker_msg_MSG_JSON_POINTER_EDIT(msg)
        self.json_pointer_store.on_broker_msg_edit(msg)

    def on_broker_msg_MSG_JSON_POINTER_DELETE(self, msg, *args):
        """ Deletes an JSON Pointer.
        """
        # Delete the pattern from its store
        self.json_pointer_store.on_broker_msg_delete(msg, *args)

        # Delete the pattern from url_data's cache and let know servers that it should be deleted from the ODB as well
        for item_id, pattern_list in self.request_dispatcher.url_data.on_broker_msg_MSG_JSON_POINTER_DELETE(msg):

            # This is a bit inefficient, if harmless, because each worker in a cluster will publish it
            # so the list of patterns will be updated that many times.

            msg = {}
            msg['action'] = broker_message.SERVICE.PUBLISH.value
            msg['service'] = 'zato.http-soap.set-audit-replace-patterns'
            msg['payload'] = {'id':item_id, 'audit_repl_patt_type':MSG_PATTERN_TYPE.JSON_POINTER.id, 'pattern_list':pattern_list}
            msg['cid'] = new_cid()
            msg['channel'] = CHANNEL.WORKER
            msg['data_format'] = DATA_FORMAT.JSON

            self.broker_client.invoke_async(msg)

# ################################################################################################################################

    def on_broker_msg_CHANNEL_HTTP_SOAP_AUDIT_RESPONSE(self, msg, *args):
        return self._on_message_invoke_service(msg, CHANNEL.AUDIT, 'SCHEDULER_JOB_EXECUTED', args)

    def on_broker_msg_CHANNEL_HTTP_SOAP_AUDIT_CONFIG(self, msg, *args):
        return self.request_dispatcher.url_data.on_broker_msg_CHANNEL_HTTP_SOAP_AUDIT_CONFIG(msg)

    def on_broker_msg_CHANNEL_HTTP_SOAP_AUDIT_STATE(self, msg, *args):
        return self.request_dispatcher.url_data.on_broker_msg_CHANNEL_HTTP_SOAP_AUDIT_STATE(msg)

    def on_broker_msg_CHANNEL_HTTP_SOAP_AUDIT_PATTERNS(self, msg, *args):
        return self.request_dispatcher.url_data.on_broker_msg_CHANNEL_HTTP_SOAP_AUDIT_PATTERNS(msg)

# ################################################################################################################################

    def _on_broker_msg_cloud_create_edit(self, msg, conn_type, config_dict, wrapper_class):

        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg['name']

        # .. delete the connection if it exists ..
        self._delete_config_close_wrapper(del_name, config_dict, conn_type, logger.debug)

        # .. and create a new one
        msg['queue_build_cap'] = float(self.server.fs_server_config.misc.queue_build_cap)
        wrapper = wrapper_class(msg)
        wrapper.build_queue()

        item = Bunch()

        config_dict[msg['name']] = item
        config_dict[msg['name']].config = msg
        config_dict[msg['name']].conn = wrapper

        return item

# ################################################################################################################################

    def on_broker_msg_CLOUD_OPENSTACK_SWIFT_CREATE_EDIT(self, msg, *args):
        """ Creates or updates an OpenStack Swift connection.
        """
        self._on_broker_msg_cloud_create_edit(msg, 'OpenStack Swift', self.worker_config.cloud_openstack_swift, SwiftWrapper)

    def on_broker_msg_CLOUD_OPENSTACK_SWIFT_DELETE(self, msg, *args):
        """ Closes and deletes an OpenStack Swift connection.
        """
        self._delete_config_close_wrapper(msg['name'], self.worker_config.cloud_openstack_swift, 'OpenStack Swift', logger.debug)

# ################################################################################################################################

    def on_broker_msg_CLOUD_AWS_S3_CREATE_EDIT(self, msg, *args):
        """ Creates or updates an AWS S3 connection.
        """
        self._update_aws_config(msg)
        self._on_broker_msg_cloud_create_edit(msg, 'AWS S3', self.worker_config.cloud_aws_s3, S3Wrapper)

    def on_broker_msg_CLOUD_AWS_S3_DELETE(self, msg, *args):
        """ Closes and deletes an AWS S3 connection.
        """
        self._delete_config_close_wrapper(msg['name'], self.worker_config.cloud_aws_s3, 'AWS S3', logger.debug)

# ################################################################################################################################

    def on_broker_msg_PUB_SUB_TOPIC_CREATE(self, msg):
        self._add_pubsub_topic(msg)

    def on_broker_msg_PUB_SUB_TOPIC_EDIT(self, msg):
        self.pubsub.update_topic(Topic(msg.name, msg.is_active, True, msg.max_depth))

    def on_broker_msg_PUB_SUB_TOPIC_DELETE(self, msg):
        self.pubsub.delete_topic(Topic(msg.name))

# ################################################################################################################################

    def _on_broker_msg_pub_sub_consumer_create_edit(self, msg):
        self.pubsub.add_consumer(
            Consumer(
                msg.client_id, msg.client_name, msg.is_active, msg.sub_key, msg.max_backlog,
                msg.delivery_mode, msg.callback_id, msg.callback_name, msg.callback_type),
            Topic(msg.topic_name))

    def _on_broker_msg_PUB_SUB_CONSUMER_CREATE(self, msg):
        self._on_broker_msg_pub_sub_consumer_create_edit(msg)

    def on_broker_msg_PUB_SUB_CONSUMER_EDIT(self, msg):
        self._on_broker_msg_pub_sub_consumer_create_edit(msg)

    def on_broker_msg_PUB_SUB_CONSUMER_DELETE(self, msg):
        self.pubsub.delete_consumer(
            Consumer(msg.client_id, msg.client_name, msg.is_active, msg.sub_key, msg.max_backlog), Topic(msg.topic_name))

# ################################################################################################################################

    def on_broker_msg_PUB_SUB_PRODUCER_CREATE(self, msg):
        self.pubsub.add_producer(Client(msg.client_id, msg.name, msg.is_active), Topic(msg.topic_name))

    def on_broker_msg_PUB_SUB_PRODUCER_EDIT(self, msg):
        self.pubsub.update_producer(Client(msg.client_id, msg.client_name, msg.is_active), Topic(msg.topic_name))

    def on_broker_msg_PUB_SUB_PRODUCER_DELETE(self, msg):
        self.pubsub.delete_producer(Client(msg.client_id, msg.client_name), Topic(msg.topic_name))

# ################################################################################################################################

    def on_broker_msg_NOTIF_RUN_NOTIFIER(self, msg):
        self._on_message_invoke_service(loads(msg.request), CHANNEL.NOTIFIER_RUN, 'NOTIF_RUN_NOTIFIER')

    def on_broker_msg_NOTIF_CLOUD_OPENSTACK_SWIFT_CREATE_EDIT(self, msg):

        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg.name

        config_dict = self.server.worker_store.worker_config.notif_cloud_openstack_swift
        config_dict.pop(del_name, None) # Delete and ignore if it doesn't exit (it's CREATE then)
        config_dict[msg.name] = Bunch()
        config_dict[msg.name].config = msg

        self._update_cloud_openstack_swift_container(msg)

        # Start a new background notifier either if it's a create action or on rename.
        if msg.source_service_type == 'create' or (old_name and old_name != msg.name):

            self._on_message_invoke_service({
                'service': 'zato.notif.invoke-run-notifier',
                'payload': {'config': msg},
                'cid': new_cid(),
            }, CHANNEL.NOTIFIER_RUN, 'NOTIF_CLOUD_OPENSTACK_SWIFT_CREATE_EDIT')

    def on_broker_msg_NOTIF_CLOUD_OPENSTACK_SWIFT_DELETE(self, msg):
        del self.server.worker_store.worker_config.notif_cloud_openstack_swift[msg.name]

# ################################################################################################################################

    def update_cassandra_conn(self, msg):
        for name in 'tls_ca_certs', 'tls_client_cert', 'tls_client_priv_key':
            value = msg.get(name)
            if value:
                value = os.path.join(self.server.repo_location, 'tls', value)
                msg[name] = value

    def on_broker_msg_DEFINITION_CASSANDRA_CREATE(self, msg):
        self.update_cassandra_conn(msg)
        self.cassandra_api.create_def(msg.name, msg)

    def on_broker_msg_DEFINITION_CASSANDRA_EDIT(self, msg):
        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg['name']
        self.update_cassandra_conn(msg)
        new_def = self.cassandra_api.edit_def(del_name, msg)
        self.cassandra_query_store.update_by_def(del_name, new_def)

    def on_broker_msg_DEFINITION_CASSANDRA_DELETE(self, msg):
        self.cassandra_api.delete_def(msg.name)

    def on_broker_msg_DEFINITION_CASSANDRA_CHANGE_PASSWORD(self, msg):
        self.cassandra_api.change_password_def(msg)

# ################################################################################################################################

    def on_broker_msg_QUERY_CASSANDRA_CREATE(self, msg):
        self.cassandra_query_api.create(msg.name, msg, def_=self.cassandra_api[msg.def_name])

    def on_broker_msg_QUERY_CASSANDRA_EDIT(self, msg):
        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg['name']
        self.cassandra_query_api.edit(del_name, msg, def_=self.cassandra_api[msg.def_name])

    def on_broker_msg_QUERY_CASSANDRA_DELETE(self, msg):
        self.cassandra_query_api.delete(msg.name)

# ################################################################################################################################

    def on_broker_msg_SEARCH_ES_CREATE(self, msg):
        self.search_es_api.create(msg.name, msg)

    def on_broker_msg_SEARCH_ES_EDIT(self, msg):
        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg['name']
        self.search_es_api.edit(del_name, msg)

    def on_broker_msg_SEARCH_ES_DELETE(self, msg):
        self.search_es_api.delete(msg.name)

# ################################################################################################################################

    def on_broker_msg_SEARCH_SOLR_CREATE(self, msg):
        self._update_queue_build_cap(msg)
        self.search_solr_api.create(msg.name, msg)

    def on_broker_msg_SEARCH_SOLR_EDIT(self, msg):
        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg['name']
        self._update_queue_build_cap(msg)
        self.search_solr_api.edit(del_name, msg)

    def on_broker_msg_SEARCH_SOLR_DELETE(self, msg):
        self.search_solr_api.delete(msg.name)

# ################################################################################################################################

    def on_broker_msg_EMAIL_SMTP_CREATE(self, msg):
        self.email_smtp_api.create(msg.name, msg)

    def on_broker_msg_EMAIL_SMTP_EDIT(self, msg):
        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg['name']
        msg.password = self.email_smtp_api.get(del_name, True).config.password
        self.email_smtp_api.edit(del_name, msg)

    def on_broker_msg_EMAIL_SMTP_DELETE(self, msg):
        self.email_smtp_api.delete(msg.name)

    def on_broker_msg_EMAIL_SMTP_CHANGE_PASSWORD(self, msg):
        self.email_smtp_api.change_password(msg)

# ################################################################################################################################

    def on_broker_msg_EMAIL_IMAP_CREATE(self, msg):
        self.email_imap_api.create(msg.name, msg)

    def on_broker_msg_EMAIL_IMAP_EDIT(self, msg):
        # It might be a rename
        old_name = msg.get('old_name')
        del_name = old_name if old_name else msg['name']
        msg.password = self.email_imap_api.get(del_name, True).config.password
        self.email_imap_api.edit(del_name, msg)

    def on_broker_msg_EMAIL_IMAP_DELETE(self, msg):
        self.email_imap_api.delete(msg.name)

    def on_broker_msg_EMAIL_IMAP_CHANGE_PASSWORD(self, msg):
        self.email_imap_api.change_password(msg)

# ################################################################################################################################
