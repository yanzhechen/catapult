# Copyright 2023 The Chromium Authors
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import absolute_import
from google.cloud import datastore

import os



_GCLOUD_PROJECT_PROD = 'chromeperf'
_GCLOUD_PROJECT_STAGE = 'chromeperf-stage'
_SKIABRIDGE_URL_PROD = 'https://skia-bridge-dot-chromeperf.appspot.com/'
_SKIABRIDGE_URL_STAGE = \
  'https://skia-bridge-dot-chromeperf-stage.uc.r.appspot.com/'
PAYLOAD_SIZE_LIMIT_KB = 1000

INTERNAL_CLIENTS = [
    'perf-chrome-internal@skia-infra-corp.iam.gserviceaccount.com',
    'perf-widevine-cdm@skia-infra-corp.iam.gserviceaccount.com',
    'perf-widevine-whitebox@skia-infra-corp.iam.gserviceaccount.com',
    'perf-v8-internal@skia-infra-corp.iam.gserviceaccount.com',
    'perf-devtools-frontend@skia-infra-corp.iam.gserviceaccount.com',
    'perf-fuchsia-internal@skia-infra-corp.iam.gserviceaccount.com',
    # All webrtc anomalies in legacy are marked as internal_only.
    'perf-webrtc-public@skia-infra-public.iam.gserviceaccount.com',
]

FUCHSIA_CLIENTS = [
  'perf-fuchsia-internal@skia-infra-corp.iam.gserviceaccount.com',
  'perf-fuchsia-public@skia-infra-public.iam.gserviceaccount.com',
]

def IsProduction():
  project = GetGcloudProject()
  if project and project == _GCLOUD_PROJECT_PROD:
    return True

  return False


def GetGcloudProject():
  return os.environ.get('GOOGLE_CLOUD_PROJECT')


def GetSkiaBridgeUrl():
  if IsProduction():
    return _SKIABRIDGE_URL_PROD

  return _SKIABRIDGE_URL_STAGE

def TestPath(key: datastore.key.Key):
  if key.kind == 'Test':
    # The Test key looks like ('Master', 'name', 'Bot', 'name', 'Test' 'name'..)
    # Pull out every other entry and join with '/' to form the path.
    return '/'.join(key.flat_path[1::2])

  assert key.kind == 'TestMetadata' or key.kind == 'TestContainer'
  return key.id_or_name

def GetFuchsiaCommitId(row, internal):
  if internal:
    commit_key = 'r_fuchsia_integ_int_git'
  else:
    commit_key = 'r_fuchsia_integ_pub_git'

  return row[commit_key]
