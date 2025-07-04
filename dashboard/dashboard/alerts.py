# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.
"""Provides the web interface for displaying an overview of alerts."""
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import datetime
from httplib2 import http
import json
import logging
import six

from google.appengine.datastore.datastore_query import Cursor
from google.appengine.ext import ndb

from dashboard import email_template
from dashboard.common import descriptor
from dashboard.common import request_handler
from dashboard.common import utils
from dashboard.models import anomaly
from dashboard.models import bug_label_patterns
from dashboard.models import skia_helper

# We need to import this last because importing it earlier causes issues with
# module lookups when importing protobuf messages before loading the
# appengine-specific shims to resolve package name lookup. This shows up as
# interesting non-deterministic issues with test loading order.
from dashboard import sheriff_config_client

_MAX_ANOMALIES_TO_COUNT = 5000
_MAX_ANOMALIES_TO_SHOW = 500


# Shows an overview of recent anomalies for perf sheriffing.
from flask import make_response, request


def AlertsHandlerGet():
  """Renders the UI for listing alerts."""
  return request_handler.RequestHandlerRenderStaticHtml('alerts.html')


def AlertsHandlerPost():
  """Returns dynamic data for listing alerts in response to XHR.

  Request parameters:
    sheriff: The name of a sheriff (optional).
    triaged: Whether to include triaged alerts (i.e. with a bug ID).
    improvements: Whether to include improvement anomalies.
    anomaly_cursor: Where to begin a paged query for anomalies (optional).

  Outputs:
    JSON data for an XHR request to show a table of alerts.
  """
  anomalies, next_cursor, count, err_msg, err_code = _GetAlerts()

  if err_msg:
    return make_response(json.dumps({'error': err_msg}), err_code)

  values = {
      'anomaly_list': AnomalyDicts(anomalies),
      'anomaly_count': count,
      'sheriff_list': _GetSheriffList(),
      'anomaly_cursor':
          (six.ensure_str(next_cursor.urlsafe()) if next_cursor else None),
      'show_more_anomalies': next_cursor != None,
  }
  request_handler.RequestHandlerGetDynamicVariables(values)
  return make_response(json.dumps(values))


def SkiaAlertsHandlerGet():
  logging.debug('[SkiaTriage] Request for getting alerts: %s', request)

  anomalies, next_cursor, _, err_msg, err_code = _GetAlerts(skia=True)
  if err_msg:
    return make_response(json.dumps({'error': err_msg}), err_code)

  values = {
      'anomaly_list':
          AnomalyDicts(anomalies, skia=True),
      'anomaly_cursor':
          (six.ensure_str(next_cursor.urlsafe()) if next_cursor else ''),
  }
  return make_response(json.dumps(values))


def _GetAlerts(skia=False):
  """ Helper function to load alerts.
  """
  sheriff_name = request.values.get('sheriff', None)
  if sheriff_name and not _SheriffIsFound(sheriff_name):
    return (None, None, None, 'Sheriff "%s" not found.' % sheriff_name,
            http.HTTPStatus.BAD_REQUEST.value)

  masters, internal_only, min_timestamp = None, None, None
  if skia:
    host = request.values.get('host', None)
    if not host:
      return (None, None, None,
              'No host found in request to filter anomlies for skia instaces',
              http.HTTPStatus.BAD_REQUEST.value)
    masters, is_internal = skia_helper.GetMastersAndIsInternalForHost(host)
    # If the request is from a internal instance, we should show both external
    # and internal data; otherwise, we should only show external data.
    if not is_internal:
      internal_only = False

    # We did 1 year of backfill for non-press benchmarks and 3 years for press
    # benchmarks. The backfill started from about June 2023. As a result, we
    # should ignore those anomalies detected for non-press benchmarks before
    # 2022/7/1, and those for press benchmarks before 2020/7/1.
    # In our scenario, we don't have info to filter the anomalies on benchmark.
    # We will use 2022/7/1 as a cut-off date, as it is not likely to look back
    # on anomalies 2.5 years ago.
    min_timestamp = datetime.datetime.strptime('2022-7-1T0:0:0',
                                               '%Y-%m-%dT%H:%M:%S')

  # Cursors are used to fetch paged queries. If none is supplied, then the
  # first 500 alerts will be returned. If a cursor is given, the next
  # 500 alerts (starting at the given cursor) will be returned.
  anomaly_cursor = request.values.get('anomaly_cursor', None)
  if anomaly_cursor:
    anomaly_cursor = Cursor(urlsafe=anomaly_cursor)

  is_improvement = None
  if not bool(request.values.get('improvements')):
    is_improvement = False

  bug_id = None
  recovered = None
  if not bool(request.values.get('triaged')):
    bug_id = ''
    recovered = False

  max_anomalies_to_show = _MAX_ANOMALIES_TO_SHOW
  if request.values.get('max_anomalies_to_show'):
    max_anomalies_to_show = int(request.values.get('max_anomalies_to_show'))

  subs = None
  if sheriff_name:
    subs = [sheriff_name]
  anomalies, next_cursor, count = anomaly.Anomaly.QueryAsync(
      start_cursor=anomaly_cursor,
      subscriptions=subs,
      bug_id=bug_id,
      is_improvement=is_improvement,
      recovered=recovered,
      count_limit=_MAX_ANOMALIES_TO_COUNT,
      limit=max_anomalies_to_show,
      master_names=masters,
      min_timestamp=min_timestamp,
      internal_only=internal_only).get_result()

  return anomalies, next_cursor, count, None, None

def _SheriffIsFound(sheriff_name):
  """Checks whether the sheriff can be found for the current user."""
  # TODO(fancl): Add an api for verifying single subscription visibility
  subscriptions = _GetSheriffList()
  return sheriff_name in subscriptions


def _GetSheriffList():
  """Returns a list of sheriff names for all sheriffs in the datastore."""
  if utils.IsStagingEnvironment():
    return []

  client = sheriff_config_client.GetSheriffConfigClient()
  subscriptions, _ = client.List(check=True)
  return [s.name for s in subscriptions]


def AnomalyDicts(anomalies, v2=False, skia=False):
  """Makes a list of dicts with properties of Anomaly entities."""
  bisect_statuses = _GetBisectStatusDict(anomalies)
  return [
      GetAnomalyDict(a, bisect_statuses.get(a.bug_id), v2, skia)
      for a in anomalies
  ]


def GetAnomalyDict(anomaly_entity, bisect_status=None, v2=False, skia=False):
  """Returns a dictionary for an Anomaly which can be encoded as JSON.

  Args:
    anomaly_entity: An Anomaly entity.
    bisect_status: String status of bisect run.

  Returns:
    A dictionary which is safe to be encoded as JSON.
  """
  test_key = anomaly_entity.GetTestMetadataKey()
  test_path = utils.TestPath(test_key)
  dashboard_link = email_template.GetReportPageLink(
      test_path, rev=anomaly_entity.end_revision, add_protocol_and_host=False)
  project_id = anomaly_entity.project_id if (
      anomaly_entity.project_id != '') else 'chromium'

  new_url = skia_helper.GetSkiaUrlForAnomaly(anomaly_entity)
  dct = {
      'bug_id': anomaly_entity.bug_id,
      'project_id': project_id,
      'dashboard_link': dashboard_link,
      'key': six.ensure_str(anomaly_entity.key.urlsafe()),
      'median_after_anomaly': anomaly_entity.median_after_anomaly,
      'median_before_anomaly': anomaly_entity.median_before_anomaly,
      'recovered': anomaly_entity.recovered,
      'units': anomaly_entity.units,
      'new_url': new_url
  }
  # On the Skia anomaly table, the anomaly model expects different properties.
  # Here we send some properties in different property name, or converge
  # multiple into on.
  if skia:
    dct['id'] = anomaly_entity.key.integer_id(
    ) or 0  # skia expected integet id.
    dct['test_path'] = test_path
    dct['is_improvement'] = anomaly_entity.is_improvement
    dct['start_revision'] = anomaly_entity.display_start or anomaly_entity.start_revision
    dct['end_revision'] = anomaly_entity.display_end or anomaly_entity.end_revision
    dct['timestamp'] = anomaly_entity.timestamp.isoformat()
    # the subscription info needed for triaging.
    subscription_names = anomaly_entity.subscription_names
    if subscription_names:
      if len(subscription_names) > 1:
        logging.warning(
            "More than one subscription names in anomaly %s. Subs: %s",
            anomaly_entity.key.id(), subscription_names)
      dct['subscription_name'] = subscription_names[0]

      subscriptions = anomaly_entity.subscriptions
      if subscriptions:
        dct['bug_component'] = subscriptions[0].bug_components[
            0] if subscriptions[0].bug_components else ''
        dct['bug_labels'] = subscriptions[0].bug_labels
        dct['bug_cc_emails'] = subscriptions[0].bug_cc_emails
  else:
    dct['improvement'] = anomaly_entity.is_improvement
    dct['start_revision'] = anomaly_entity.start_revision
    dct['end_revision'] = anomaly_entity.end_revision

  if v2:
    bug_labels = set()
    bug_components = set()
    if anomaly_entity.internal_only:
      bug_labels.add('Restrict-View-Google')
    tags = set(bug_label_patterns.GetBugLabelsForTest(test_key))
    subscriptions = list(anomaly_entity.subscriptions)
    tags.update([l for s in subscriptions for l in s.bug_labels])
    bug_components = set(c for s in subscriptions for c in s.bug_components)
    for tag in tags:
      if tag.startswith('Cr-'):
        bug_components.add(tag.replace('Cr-', '').replace('-', '>'))
      else:
        bug_labels.add(tag)

    dct['bug_components'] = list(bug_components)
    dct['bug_labels'] = list(bug_labels)

    desc = descriptor.Descriptor.FromTestPathSync(test_path)
    dct['descriptor'] = {
        'testSuite': desc.test_suite,
        'measurement': desc.measurement,
        'bot': desc.bot,
        'testCase': desc.test_case,
        'statistic': desc.statistic,
    }
    dct['pinpoint_bisects'] = anomaly_entity.pinpoint_bisects
  else:
    test_path_parts = test_path.split('/')
    dct['absolute_delta'] = '%s' % anomaly_entity.GetDisplayAbsoluteChanged()
    dct['bisect_status'] = bisect_status
    dct['bot'] = test_path_parts[1]
    dct['date'] = str(anomaly_entity.timestamp.date())
    dct['display_end'] = anomaly_entity.display_end
    dct['display_start'] = anomaly_entity.display_start
    dct['master'] = test_path_parts[0]
    dct['percent_changed'] = '%s' % anomaly_entity.GetDisplayPercentChanged()
    dct['ref_test'] = anomaly_entity.GetRefTestPath()
    dct['test'] = '/'.join(test_path_parts[3:])
    dct['testsuite'] = test_path_parts[2]
    dct['timestamp'] = anomaly_entity.timestamp.isoformat()
    dct['type'] = 'anomaly'

  return dct


def _GetBisectStatusDict(anomalies):
  """Returns a dictionary of bug ID to bisect status string."""
  bug_id_list = {a.bug_id for a in anomalies if a.bug_id and a.bug_id > 0}
  bugs = ndb.get_multi(ndb.Key('Bug', b) for b in bug_id_list)
  return {b.key.id(): b.latest_bisect_status for b in bugs if b}


def SkiaLoadSheriffConfigsHandlerGet():
  try:
    logging.debug('[SkiaTriage] Load sheriff configs requests.')
    sheriff_config_names = _GetSheriffList()
    logging.debug('[SkiaTriage] _GetSheriffList returned  %d configs.',
                  len(sheriff_config_names))
  except sheriff_config_client.InternalServerError as e:
    return make_response(json.dumps({'error': str(e)}))
  return make_response(json.dumps({'sheriff_list': sheriff_config_names}))
