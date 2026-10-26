"""EXPERIMENTAL: uses Loom's unofficial API and may break."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

CLIENT_ID = os.environ.get("TOUCAN_LOOM_CLIENT_ID", "zjAE9OPtU9ercrK02sZowEPwoPlrRPTg")
AUTH_BASE = "https://auth.atlassian.com"
SCOPES = "read:loom:twg-cli write:loom:twg-cli offline_access"
CONFIG = Path.home() / ".config/toucan/loom"
CREDENTIALS = CONFIG / "oauth.json"
GRAPHQL = "https://api.atlassian.com/graphql"
PART_SIZE = 5 * 1024 * 1024
CREATE_UPLOAD = """mutation TwgCLI_LoomCreateUploadCredentials($siteId: ID!) {
  loom_createFileUploadCredentials(siteId: $siteId) {
    success credentials { __typename
      ... on LoomS3UploadInfo { credentialType accessKeyId bucket durationSeconds region path secretAccessKey sessionToken endpoint }
      ... on LoomObjectStoreUploadInfo { credentialType url hostname bucket workspaceType siteId activationId token { value expiration } }
    }
  }
}"""
COMPLETE_UPLOAD = """mutation TwgCLI_LoomCompleteVideoUpload($input: LoomCompleteVideoFileUploadInput!) {
  loom_completeVideoFileUpload(input: $input) { success video { id name url isComplete playableDuration } }
}"""
SPACE_QUERY = """query TwgCLI_LoomSpaceActivation($siteId: ID!) {
  loom_spacesSearch(siteId: $siteId) { id }
}"""



class AuthError(Exception):
    pass


def request(path, fields):
    req = urllib.request.Request(
        AUTH_BASE + path,
        data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read())
        except (ValueError, OSError):
            raise AuthError(f"Atlassian authentication returned HTTP {error.code}.") from None
        if not isinstance(result, dict):
            raise AuthError(f"Atlassian authentication returned HTTP {error.code}.") from None
        result["_http_status"] = error.code
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise AuthError("Could not reach Atlassian authentication. No credentials were changed.") from None
    if not isinstance(result, dict):
        raise AuthError("Atlassian returned an invalid authentication response.")
    return result


def provider_error(result):
    # Provider descriptions can contain user-controlled or credential material.
    code = result.get("error", "invalid_response")
    if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", code):
        code = "authentication_failed"
    return AuthError(f"Atlassian authentication failed: {code}. Run toucan-upload-loom login to authenticate again.")


def prepare_directory():
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    CONFIG.mkdir(exist_ok=True, mode=0o700)
    CONFIG.chmod(0o700)


def lock():
    prepare_directory()
    fd = os.open(CONFIG / "auth.lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return os.fdopen(fd, "w")


def token_record(response, previous=None):
    if response.get("error"):
        raise provider_error(response)
    access = response.get("access_token")
    lifetime = response.get("expires_in")
    refresh = response.get("refresh_token") or (previous or {}).get("refresh_token")
    if not isinstance(access, str) or not access or not isinstance(lifetime, (int, float)) or lifetime <= 0:
        raise AuthError("Atlassian returned incomplete credentials.")
    if str(response.get("token_type", "")).lower() != "bearer":
        raise AuthError("Atlassian returned an unsupported token type.")
    if not isinstance(refresh, str) or not refresh:
        raise AuthError("Atlassian did not grant offline access. Persistent login was not saved.")
    try:
        segment = access.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        audiences = claims.get("aud", [])
        audiences = audiences if isinstance(audiences, list) else [audiences]
        sites = sorted({m.group(1).lower() for value in audiences if isinstance(value, str)
                        if (m := re.fullmatch(r"ari:cloud:[a-z0-9-]+::site/([0-9a-fA-F-]{36})", value))})
    except (ValueError, IndexError, TypeError):
        raise AuthError("Could not identify the authorized site from the OAuth token.") from None
    if len(sites) != 1:
        raise AuthError("Authorize exactly one Loom site before saving this login.")
    return {"access_token": access, "refresh_token": refresh,
            "expires_at": time.time() + lifetime, "cloud_id": sites[0],
            "scope": response.get("scope", SCOPES)}


def save(record):
    prepare_directory()
    fd, temp = tempfile.mkstemp(prefix="oauth-", dir=CONFIG)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, CREDENTIALS)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def login(force=False):
    with lock():
        if CREDENTIALS.exists() and not force:
            raise AuthError("A login is already saved. Use toucan-upload-loom status, or toucan-upload-loom login --force to reauthenticate.")
        response = request("/oauth/device/code", {"client_id": CLIENT_ID, "scope": SCOPES})
        if response.get("error"):
            raise provider_error(response)
        code = response.get("device_code")
        url = response.get("verification_uri_complete") or response.get("verification_uri")
        expiry = response.get("expires_in")
        if not isinstance(code, str) or not isinstance(url, str) or not isinstance(expiry, (int, float)):
            raise AuthError("Atlassian returned an incomplete device authorization.")
        if not url.startswith("https://auth.atlassian.com/"):
            raise AuthError("Atlassian returned an unexpected authorization URL.")
        print("Open this authorization URL in your browser:", flush=True)
        print(url, flush=True)
        print("Authorize your Loom workspace.", flush=True)
        interval = max(float(response.get("interval", 5)), 5)
        deadline = time.monotonic() + min(float(expiry), 900)
        while time.monotonic() < deadline:
            time.sleep(interval)
            token = request("/oauth/token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                             "client_id": CLIENT_ID, "device_code": code})
            error = token.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error:
                raise provider_error(token)
            record = token_record(token)
            save(record)
            print("Loom login saved. Automatic refresh is available.", flush=True)
            return
        raise AuthError("The authorization window expired. Run toucan-upload-loom login again.")


def credentials(force_refresh=False):
    with lock():
        if not CREDENTIALS.exists():
            raise AuthError("No Loom login is saved. Run toucan-upload-loom login first.")
        with CREDENTIALS.open() as source:
            record = json.load(source)
        if force_refresh or record["expires_at"] <= time.time() + 120:
            response = request("/oauth/token", {"grant_type": "refresh_token", "client_id": CLIENT_ID,
                                                "refresh_token": record["refresh_token"]})
            record = token_record(response, record)
            save(record)
        return record


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(NoRedirect)


def fetch(url, method="GET", headers=None, data=None, timeout=60):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise AuthError("Refusing an upload endpoint without HTTPS.")
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with HTTP.open(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise AuthError(f"{method} request failed with HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AuthError(f"{method} request failed due to a network error.") from None


def gql(record, query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    raw = fetch(GRAPHQL, "POST", {"Content-Type": "application/json",
                "Authorization": "Bearer " + record["access_token"]}, body)
    result = json.loads(raw)
    if result.get("errors"):
        raise AuthError("Loom API rejected the operation.")
    if not isinstance(result.get("data"), dict):
        raise AuthError("Loom API returned an incomplete response.")
    return result["data"]


def activation(record):
    spaces = gql(record, SPACE_QUERY, {"siteId": record["cloud_id"]})["loom_spacesSearch"]
    values = {match.group(1) for space in spaces
              if (match := re.search(r":space/activation/([^/]+)/", space["id"]))}
    if len(values) != 1:
        raise AuthError("Could not identify exactly one authorized Loom workspace.")
    return values.pop()


def with_token(url, token, **parameters):
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    query.update(authToken=token, **parameters)
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def object_upload(path, credentials, content_type, key, cloud_id):
    try:
        token = credentials["token"]["value"]
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        prefix = claims["oid"].split(".*")[0].split(".+")[0].strip()
    except (KeyError, IndexError, ValueError):
        raise AuthError("Loom returned an invalid storage credential.") from None
    if not prefix or credentials["siteId"] != cloud_id:
        raise AuthError("Loom returned storage credentials for an unexpected site.")
    base = credentials["hostname"].rstrip("/")
    pieces = [credentials[k] for k in ("bucket", "workspaceType", "siteId", "activationId")]
    base += "/" + urllib.parse.quote(pieces[0], safe="") + "/upload/" + "/".join(urllib.parse.quote(p, safe="") for p in pieces[1:]) + "/multipart"
    routing = base64.b64encode(json.dumps({"entityType": "site", "entityId": credentials["siteId"]}, separators=(",", ":")).encode()).decode().rstrip("=")
    routed = {"X-Loom-Entity-Routing": routing}
    start = with_token(base, token, objectId=prefix + key, contentType=content_type, shouldOverwrite="false")
    initiated = json.loads(fetch(start, "POST", {**routed, "x-content-checksum-type": "SHA256", "x-time-to-live": "PT24H"}, b""))
    upload_id = initiated.get("uploadId")
    if not isinstance(upload_id, str) or not upload_id:
        raise AuthError("Loom storage returned no upload ID.")
    upload_base = base + "/" + urllib.parse.quote(upload_id, safe="")
    size = path.stat().st_size
    count = (size + PART_SIZE - 1) // PART_SIZE

    def part(number):
        with path.open("rb") as source:
            source.seek((number - 1) * PART_SIZE)
            data = source.read(min(PART_SIZE, size - (number - 1) * PART_SIZE))
        expected = min(PART_SIZE, size - (number - 1) * PART_SIZE)
        if len(data) != expected:
            raise AuthError("The source video changed during upload.")
        headers = {**routed, "Content-Type": "application/octet-stream", "Content-Length": str(len(data)),
                   "x-content-checksum": base64.b64encode(hashlib.sha256(data).digest()).decode(),
                   "x-content-checksum-type": "SHA256", "x-part-number": str(number)}
        for attempt in range(3):
            try:
                fetch(with_token(upload_base + "/parts", token), "PUT", headers, data, timeout=180)
                return
            except AuthError as error:
                # Replacing the same numbered part is idempotent; only transient failures retry.
                transient = "network error" in str(error) or any(f"HTTP {code}" in str(error) for code in (429, 500, 502, 503, 504))
                if not transient or attempt == 2:
                    raise
                time.sleep(0.5 * (2 ** attempt))
    try:
        with ThreadPoolExecutor(max_workers=min(4, count)) as executor:
            list(executor.map(part, range(1, count + 1)))
        fetch(with_token(upload_base + "/complete", token), "POST", routed, b"", timeout=180)
    except BaseException:
        try:
            fetch(with_token(upload_base, token), "DELETE", routed)
        except AuthError:
            pass
        raise
    return key, {"mode": "tdp-multipart", "bytes": size, "parts": count}


def s3_upload(path, credentials, content_type, key):
    endpoint = credentials.get("endpoint", "").strip()
    if endpoint and not endpoint.startswith("https://"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", endpoint):
            raise AuthError("Loom returned an invalid S3 endpoint.")
        key = endpoint + "/" + key
        endpoint = ""
    endpoint = endpoint or f"https://{credentials['bucket']}.s3.{credentials['region']}.amazonaws.com"
    prefix = credentials["path"].rstrip("/") + "/"
    if not prefix.startswith("uploads/"):
        raise AuthError("Loom returned an unsupported S3 path.")
    full_key = prefix + key
    completion_key = full_key[len("uploads/"):].split("/", 1)[1]
    url = endpoint.rstrip("/") + "/" + "/".join(urllib.parse.quote(p, safe="") for p in full_key.split("/"))
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    payload_hash = digest.hexdigest()
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parsed = urllib.parse.urlsplit(url)
    headers = {"content-type": content_type, "host": parsed.netloc, "x-amz-content-sha256": payload_hash,
               "x-amz-date": now, "x-amz-security-token": credentials["sessionToken"]}
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(k + ":" + headers[k].strip() + "\n" for k in sorted(headers))
    canonical = "\n".join(["PUT", parsed.path, parsed.query, canonical_headers, signed, payload_hash])
    scope = f"{now[:8]}/{credentials['region']}/s3/aws4_request"
    string = "\n".join(["AWS4-HMAC-SHA256", now, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    signing_key = ("AWS4" + credentials["secretAccessKey"]).encode()
    for value in (now[:8], credentials["region"], "s3", "aws4_request"):
        signing_key = hmac.new(signing_key, value.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, string.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={credentials['accessKeyId']}/{scope}, SignedHeaders={signed}, Signature={signature}"
    headers["Content-Length"] = str(path.stat().st_size)
    with path.open("rb") as source:
        fetch(url, "PUT", headers, source, timeout=600)
    return completion_key, {"mode": "s3", "bytes": path.stat().st_size}


def upload(record, args):
    parser = argparse.ArgumentParser(prog="toucan-upload-loom upload")
    parser.add_argument("file", type=Path)
    parser.add_argument("--title")
    parser.add_argument("--content-type", default="video/mp4")
    options = parser.parse_args(args)
    path = options.file.resolve()
    if not path.is_file() or path.stat().st_size <= 0 or not path.suffix:
        raise AuthError("Choose a readable, nonempty video file with an extension.")
    if path.stat().st_size > 4 * 1024 ** 3:
        raise AuthError("The video exceeds Loom's 4 GB upload limit.")
    filename = options.title.strip() if options.title else path.name
    if not Path(filename).suffix:
        filename += path.suffix
    if len(filename) > 255:
        raise AuthError("The video title must be at most 255 characters.")
    workspace = activation(record)
    created = gql(record, CREATE_UPLOAD, {"siteId": record["cloud_id"]})["loom_createFileUploadCredentials"]
    if not created or not created.get("success") or not created.get("credentials"):
        raise AuthError("Loom did not issue upload credentials.")
    storage = created["credentials"]
    key = uuid.uuid4().hex + path.suffix.lower()
    if storage["__typename"] == "LoomObjectStoreUploadInfo":
        if storage["activationId"] != workspace:
            raise AuthError("Loom returned storage credentials for another workspace.")
        key_path, _ = object_upload(path, storage, options.content_type, key, record["cloud_id"])
    elif storage["__typename"] == "LoomS3UploadInfo":
        key_path, _ = s3_upload(path, storage, options.content_type, key)
    else:
        raise AuthError("Loom returned an unsupported upload transport.")
    completion = {"workspaceId": f"ari:cloud:loom:{record['cloud_id']}:workspace/{workspace}", "keyPath": key_path, "fileName": filename}
    completed = gql(record, COMPLETE_UPLOAD, {"input": completion})["loom_completeVideoFileUpload"]
    if not completed or not completed.get("success") or not completed.get("video"):
        raise AuthError("Loom did not confirm publishing. Check the library before retrying.")
    video = completed["video"]
    if not isinstance(video.get("url"), str) or not video["url"].startswith("https://") or not isinstance(video.get("id"), str):
        raise AuthError("Loom returned an invalid video result.")
    print(json.dumps({"url": video["url"], "id": video["id"]}))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="toucan-upload-loom")
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("login")
    auth.add_argument("--force", action="store_true")
    commands.add_parser("status")
    send = commands.add_parser("upload")
    send.add_argument("file")
    send.add_argument("--title")
    options = parser.parse_args(argv)
    try:
        if options.command == "login":
            login(force=options.force)
        elif options.command == "status":
            if not CREDENTIALS.is_file():
                print(json.dumps({"logged_in": False}))
                return 1
            record = credentials()
            print(json.dumps({"logged_in": True, "workspace": activation(record)}))
        else:
            args = [options.file]
            if options.title is not None:
                args += ["--title", options.title]
            upload(credentials(), args)
        return 0
    except (AuthError, KeyError, ValueError, TypeError, OSError, IndexError, AttributeError) as error:
        if options.command == "status":
            print(json.dumps({"logged_in": False}))
        else:
            message = str(error) if isinstance(error, AuthError) else "Invalid local authentication state or upload response."
            print("Loom error: " + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
