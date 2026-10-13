import base64
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import urllib.parse
import zipfile

import pytest

import aptoide_recovery as ar

PACKAGE = 'com.example.game'
DEVICE = {'serial': 'test-device', 'sdk': 35, 'abis': ['arm64-v8a', 'armeabi-v7a'], 'density': 480}
SHA1 = 'a' * 40


def apk_bytes(abi='arm64-v8a'):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as z:
        z.writestr('AndroidManifest.xml', b'manifest')
        z.writestr(f'lib/{abi}/libgame.so', b'library')
    return out.getvalue()


def item(name='base', payload=None):
    payload = apk_bytes() if payload is None else payload
    return {'name': name, 'path': 'https://pool.apk.aptoide.com/test.apk',
            'filesize': len(payload), 'md5sum': hashlib.md5(payload).hexdigest()}


def metadata(splits=True):
    return {'package': PACKAGE, 'store': {'name': 'appupdater'},
            'file': {**item(), 'vername': '1.0', 'malware': {'rank': 'TRUSTED'},
                     'signature': {'sha1': SHA1}, 'hardware': {'sdk': 24}},
            'aab': {'splits': [item('config.arm64_v8a')] if splits else []}}


def response(body):
    return io.BytesIO(json.dumps({'nodes': {'meta': {'data': body}}}).encode())


def test_metadata_encodes_actual_device_and_enables_splits():
    with patch.object(ar.urllib.request, 'urlopen', return_value=response(metadata())) as fetch:
        data, files = ar._metadata(PACKAGE, DEVICE)
    url = fetch.call_args.args[0].full_url
    assert '/aab=1?' in url
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['q'][0]
    spec = base64.urlsafe_b64decode(q + '=' * (-len(q) % 4)).decode()
    assert 'myCPU=arm64-v8a' in spec and 'maxSdk=35' in spec and 'myDensity=480' in spec
    assert len(files) == 2 and data['package'] == PACKAGE


@pytest.mark.parametrize('change', ['package', 'store', 'rank', 'sdk', 'cpu', 'split_cpu', 'missing_split', 'duplicate_split'])
def test_rejects_untrusted_incompatible_or_incomplete_metadata(change):
    data = metadata()
    if change == 'package': data['package'] = 'com.other.game'
    if change == 'store': data['store']['name'] = 'community'
    if change == 'rank': data['file']['malware']['rank'] = 'UNKNOWN'
    if change == 'sdk': data['file']['hardware']['sdk'] = 36
    if change == 'cpu': data['file']['hardware']['cpus'] = ['x86_64']
    if change == 'split_cpu': data['aab']['splits'][0]['name'] = 'config.x86_64'
    if change == 'missing_split': data['aab']['required_splits'] = ['feature.missing']
    if change == 'duplicate_split': data['aab']['splits'] *= 2
    with patch.object(ar.urllib.request, 'urlopen', return_value=response(data)), pytest.raises(ValueError):
        ar._metadata(PACKAGE, DEVICE)


def test_non_split_apk_with_null_aab():
    data = metadata(False); data['aab'] = None
    with patch.object(ar.urllib.request, 'urlopen', return_value=response(data)):
        _, files = ar._metadata(PACKAGE, DEVICE)
    assert len(files) == 1


@pytest.mark.parametrize('failure', ['md5', 'size', 'package', 'cpu', 'zip'])
def test_download_rejects_corrupt_wrong_package_or_cpu(tmp_path, failure):
    payload = b'not a zip' if failure == 'zip' else apk_bytes('x86_64' if failure == 'cpu' else 'arm64-v8a')
    details = item(payload=payload)
    if failure == 'md5': details['md5sum'] = '0' * 32
    if failure == 'size': details['filesize'] += 1
    package = 'com.other.game' if failure == 'package' else PACKAGE
    with patch.object(ar.urllib.request, 'urlopen', return_value=io.BytesIO(payload)), \
         patch.object(ar, 'extract_package_name_from_artifact', return_value=package), \
         pytest.raises((ValueError, zipfile.BadZipFile)):
        ar._download(details, tmp_path / 'app.apk', PACKAGE, DEVICE, None)


@pytest.mark.parametrize('prefix', ['Signer #1', 'Signer (minSdkVersion=33, maxSdkVersion=2147483647)', 'V3.0 Signer:', 'V3.1 Signer: (minSdkVersion=33, maxSdkVersion=2147483647)'])
def test_signature_validates_device_sdk_and_supports_rotated_certificate(prefix):
    stdout = f'{prefix} certificate SHA-256 digest: ' + 'b' * 64 + f'\n{prefix} certificate SHA-1 digest: ' + SHA1
    with patch.object(ar.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr='')) as run:
        certs = ar._verify_signature('app.apk', DEVICE, SHA1, ('apksigner', {}))
    assert certs == frozenset({'b' * 64})
    cmd = run.call_args.args[0]
    assert cmd[cmd.index('--min-sdk-version') + 1] == '35'
    assert cmd[cmd.index('--max-sdk-version') + 1] == '35'


@pytest.mark.parametrize('returncode,sha1', [(1, SHA1), (0, 'c' * 40)])
def test_bad_signature_or_fingerprint_rejected(returncode, sha1):
    out = 'Signer #1 certificate SHA-256 digest: ' + 'b' * 64 + '\nSigner #1 certificate SHA-1 digest: ' + sha1
    with patch.object(ar.subprocess, 'run', return_value=SimpleNamespace(returncode=returncode, stdout=out, stderr='invalid')), pytest.raises(ValueError):
        ar._verify_signature('app.apk', DEVICE, SHA1, ('apksigner', {}))


@pytest.mark.parametrize('failure', [None, 'download', 'signature', 'split_signature', 'device_changed', 'cancel'])
def test_all_artifacts_validated_before_install_and_failures_never_install(failure):
    data = metadata()
    items = [{**data['file'], 'name': 'base'}] + data['aab']['splits']
    stopped = [False]
    def download(item, path, *args):
        if failure == 'download': raise ValueError('bad md5')
        Path(path).write_bytes(apk_bytes())
        if failure == 'cancel': stopped[0] = True
    cert = frozenset({'b' * 64})
    signatures = [cert, frozenset({'c' * 64})] if failure == 'split_signature' else [cert, cert]
    devices = [DEVICE, {**DEVICE, 'serial': 'other'}] if failure == 'device_changed' else [DEVICE, DEVICE]
    def install(path, **kwargs):
        with zipfile.ZipFile(path) as archive:
            assert archive.namelist() == ['base.apk', 'split_1.apk']
        return True, 'accepted'
    with patch.object(ar, '_device_spec', side_effect=devices), \
         patch.object(ar, '_signature_tool', return_value=('signer', {})), \
         patch.object(ar, '_metadata', return_value=(data, items)), \
         patch.object(ar, '_download', side_effect=download), \
         patch.object(ar, '_verify_signature', side_effect=ValueError('bad signature') if failure == 'signature' else signatures) as verify, \
         patch.object(ar, 'push_apk_with_acceptance', side_effect=install) as push:
        result = ar.download_and_install_aptoide(PACKAGE, cancelled=lambda: stopped[0])
    assert result['ok'] is (failure is None)
    if failure is None:
        assert verify.call_count == 2 and result['file_count'] == 2
        push.assert_called_once()
    else:
        push.assert_not_called()


def test_cancelled_before_any_device_or_network_access():
    with patch.object(ar, '_device_spec') as device, patch.object(ar.urllib.request, 'urlopen') as fetch:
        result = ar.download_and_install_aptoide(PACKAGE, cancelled=lambda: True)
    assert not result['ok']
    device.assert_not_called(); fetch.assert_not_called()


def test_missing_signature_tool_skips_without_download_or_install():
    with patch.object(ar, '_device_spec', return_value=DEVICE), \
         patch.object(ar, '_signature_tool', side_effect=ValueError('missing tool')), \
         patch.object(ar, '_metadata') as meta, patch.object(ar, 'push_apk_with_acceptance') as push:
        assert not ar.download_and_install_aptoide(PACKAGE)['ok']
    meta.assert_not_called(); push.assert_not_called()
