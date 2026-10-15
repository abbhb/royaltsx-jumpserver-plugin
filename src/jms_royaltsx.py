#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JumpServer -> Royal TSX Dynamic Folder.

Reads the assets a JumpServer account is allowed to reach, together with the
accounts permitted on each of them, and emits RoyalJSON on stdout.  Royal TSX
turns that into a browsable tree of SSH connections, so a machine can be
reached with one click instead of walking the JumpServer TUI menu.

stdout is reserved for RoyalJSON -- anything Royal TSX prints there is parsed,
so all diagnostics go to stderr.

The SSH connections this emits use koko's "direct login" username format:

    <jumpserver_user>@<account_username>@<asset_ip_or_id>

koko (JumpServer's SSH server) parses that username and proxies straight to the
asset, skipping the interactive menu.  The SSH password is the user's ordinary
JumpServer password.  See pkg/auth/ssh.go in jumpserver/koko for the format.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import hmac
import html
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "royaltsx-jumpserver")
# JMS_ROYALTSX_CONFIG lets you point at a different config without touching the
# installed one -- handy for trying settings out.
CONFIG_PATH = os.environ.get("JMS_ROYALTSX_CONFIG") or os.path.join(CONFIG_DIR, "config.json")
SCRIPT_PATH = os.path.join(CONFIG_DIR, "jms_royaltsx.py")

DEFAULT_CONFIG = {
    "base_url": "",
    "koko": {"host": "", "port": 2222},
    "auth": {
        "mode": "password",
        "username": "",
        "password": "",
        "private_token": "",
        "access_key_id": "",
        "access_key_secret": "",
    },
    "password_source": "royaltsx",
    "keychain_service": "royaltsx-jumpserver",
    "org_id": "",
    "verify_tls": True,
    "options": {
        "group_by": "asset",
        "include_accounts": True,
        "skip_manual_accounts": True,
        "separator": "auto",
        "use_uuid_as_target": False,
        "workers": 8,
        "timeout": 20,
        "path_prefix": "",
    },
    # How the generated SSH connections authenticate to koko. The JumpServer
    # credential above is what the *API* login uses; this is separate, because
    # plenty of people SSH to the bastion with a key and never a password.
    "connection_auth": {
        "mode": "password",
        "key_file": "",
        "key_passphrase": "",
    },
    # Terminal behaviour for the generated connections. Keeping the content and
    # the tab makes it possible to scroll back after `exit` or a dropped link.
    # The companion *application* setting "Keep remotely disconnected tabs
    # open" has to be enabled once in Royal TSX's Settings.
    "terminal": {
        "keep_content_on_disconnect": True,
        "show_history_after_disconnect": True,
    },
}

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def looks_like_uuid(text):
    return bool(UUID_RE.match((text or "").strip()))


def log(msg):
    """Diagnostics go to stderr; stdout belongs to RoyalJSON."""
    print(msg, file=sys.stderr)


def die(msg):
    log("错误: " + msg)
    raise SystemExit(1)


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

def deep_merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    if not os.path.exists(CONFIG_PATH):
        die(
            "找不到配置文件 {0}\n"
            "请先运行仓库里的 install.sh 生成配置，或手动创建该文件。".format(CONFIG_PATH)
        )
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except ValueError as exc:
        die("配置文件不是合法的 JSON: {0}".format(exc))
    cfg = deep_merge(DEFAULT_CONFIG, raw)
    if not cfg["base_url"]:
        die("配置项 base_url 未设置（例如 https://jumpserver.example.com）")
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    if not cfg["koko"]["host"]:
        # The SSH entry point usually lives on the same host as the web UI,
        # but it is a distinct port and may be a different name entirely.
        cfg["koko"]["host"] = urllib.parse.urlparse(cfg["base_url"]).hostname or ""
    return cfg


def credential_debug_hint():
    """Say where a credential was looked for. Names only, never values, so a
    failure can be diagnosed without leaking the password into a log."""
    lines = []

    names = sorted(
        key for key in os.environ
        if any(hint in key.lower()
               for hint in ("dynfolder", "dyncredential", "effective"))
    )
    if names:
        lines.append("环境变量里有（只看名字）：{0}".format(", ".join(names)))
    else:
        lines.append("没有看到任何 DynFolder_* / Effective* 环境变量。")

    stdin_username, stdin_password = _STDIN_CREDENTIALS or (None, None)
    if stdin_password:
        lines.append("stdin 里收到了凭据。")
    elif os.environ.get("JMS_CREDS_ON_STDIN") != "1":
        lines.append("脚本没有从 bootstrap 拿到标记（JMS_CREDS_ON_STDIN），"
                     "所以没去读 stdin。\n"
                     "  常见原因：动态文件夹里还是旧版导入的脚本正文。"
                     "用最新的 dist/JumpServer.rdfe 重新导入一次即可。")
    elif stdin_username:
        lines.append("stdin 里只收到用户名，没有密码——"
                     "说明 Credentials 页的密码没填，或者密码 token 没被替换。")
    else:
        lines.append("stdin（heredoc）里什么都没收到——"
                     "说明脚本正文里的凭据 token 没有被替换。")
    return "  " + "\n  ".join(lines)


def no_password_help():
    return (
        "没有取到 JumpServer 密码。两种给法，任选其一：\n"
        "  1) 推荐：在 Royal TSX 里选中这个动态文件夹 → Credentials 页 →\n"
        "     选 'Specify username and password'，把用户名和密码填进去。\n"
        "     密码会加密存在文档里，然后右键文件夹 Reload 即可。\n"
        "  2) 或者改配置文件里的 password_source，用钥匙串 / 明文 / 环境变量。\n"
        "排查线索：\n" + credential_debug_hint()
    )


def credentials_from_royaltsx():
    """Read the dynamic folder's own credential out of the environment.

    When the folder's Advanced settings use "Environment Variables" mode,
    Royal TSX exposes its replacement tokens to the script as variables. The
    prefix is configurable, so match on the shape of the name rather than on a
    fixed string.
    """
    username = password = None

    # "EffectivePassword" is distinctive enough to match on its own, with any
    # prefix the user may have configured.
    for key, value in os.environ.items():
        flat = re.sub(r"[^a-z]", "", key.lower())
        if flat.endswith("effectivepassword"):
            password = password or value
        elif flat.endswith("effectiveusername"):
            username = username or value

    if password or username:
        return username, password

    # Otherwise accept a shorter name, but only from a variable that is
    # recognisably Royal TSX's.
    for key, value in os.environ.items():
        flat = re.sub(r"[^a-z]", "", key.lower())
        if not any(hint in flat for hint in ("dynfolder", "dyncredential")):
            continue
        if flat.endswith("password"):
            password = password or value
        elif flat.endswith("username"):
            username = username or value
    return username, password


_STDIN_CREDENTIALS = None


def credentials_from_stdin():
    """Read the credential the bootstrap heredoc put on stdin.

    Royal TSX substitutes the tokens into the script text before running it;
    the bootstrap hands them over on stdin so nothing is ever shell-parsed.
    The result is cached because stdin can only be read once.

    Reading is gated on a marker the bootstrap sets, so running the script by
    hand can never block on an inherited pipe that nobody is going to write to.
    """
    global _STDIN_CREDENTIALS
    if _STDIN_CREDENTIALS is not None:
        return _STDIN_CREDENTIALS

    _STDIN_CREDENTIALS = (None, None)
    if os.environ.get("JMS_CREDS_ON_STDIN") != "1":
        return _STDIN_CREDENTIALS
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return _STDIN_CREDENTIALS
        data = sys.stdin.read()
    except (OSError, ValueError):
        return _STDIN_CREDENTIALS

    if not data.strip():
        return _STDIN_CREDENTIALS

    username, _, password = data.partition("\n")
    if password.endswith("\n"):
        password = password[:-1]      # drop the newline the heredoc adds
    username = username.strip()

    # If substitution did not happen we are looking at the raw markers; treat
    # that as "nothing configured" rather than using them as a password.
    if "$Effective" in username or "$Effective" in password:
        return _STDIN_CREDENTIALS

    _STDIN_CREDENTIALS = (username or None, password or None)
    return _STDIN_CREDENTIALS


def resolve_password(cfg, source=None, strict=True):
    """Fallback password sources, for when the credential is not set in the
    dynamic folder itself."""
    auth = cfg["auth"]
    source = (source or cfg.get("password_source") or "royaltsx").lower()
    if source == "config":
        return auth.get("password") or ""
    if source == "none":
        return ""
    if source == "env":
        return os.environ.get("JMS_PASSWORD", "")
    if source == "keychain":
        user = auth.get("username") or ""
        service = cfg.get("keychain_service") or "royaltsx-jumpserver"
        try:
            out = subprocess.run(
                ["/usr/bin/security", "find-generic-password",
                 "-s", service, "-a", user, "-w"],
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if strict:
                die("读取钥匙串失败: {0}".format(exc))
            return ""
        if out.returncode != 0:
            if strict:
                die(
                    "钥匙串里找不到 {0} 的密码（service={1}）。\n"
                    "可以这样写入:\n"
                    "  security add-generic-password -s {1} -a {0} -w '<密码>' -U"
                    .format(user, service)
                )
            return ""
        return out.stdout.strip()
    if strict:
        die("未知的 password_source: {0}（可选 royaltsx / auto / config / env / keychain / none）"
            .format(source))
    return ""


def resolve_credentials(cfg):
    """Return (username, password) for the API login and for the SSH
    connections -- koko checks the same password against the direct-login
    username.

    The credential you set on the dynamic folder in Royal TSX's GUI always
    wins: it is the encrypted, visual path. password_source only decides what
    to fall back to when the GUI credential is empty.
    """
    auth = cfg["auth"]
    source = (cfg.get("password_source") or "royaltsx").lower()

    if source != "none":
        # Environment variables first (if the folder is configured that way),
        # then the heredoc on stdin (the default, inline substitution).
        env_username, env_password = credentials_from_royaltsx()
        if not env_password:
            env_username, env_password = credentials_from_stdin()
        if env_password:
            return env_username or auth.get("username") or "", env_password
        if source == "royaltsx":
            # Nothing set in the GUI yet; let the caller report that plainly
            # rather than reaching for a file the user never configured.
            return env_username or auth.get("username") or "", ""

    if source == "auto":
        for candidate in ("keychain", "config"):
            value = resolve_password(cfg, candidate, strict=False)
            if value:
                return auth.get("username") or "", value
        return auth.get("username") or "", ""

    return auth.get("username") or "", resolve_password(cfg, source)


# --------------------------------------------------------------------------- #
# JumpServer API client
# --------------------------------------------------------------------------- #

class JmsClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg["base_url"]
        self.timeout = int(cfg["options"].get("timeout") or 20)
        self.token = None
        self.username = cfg["auth"].get("username") or ""
        self.auth_header = None
        if cfg["verify_tls"]:
            self.ssl_ctx = ssl.create_default_context()
        else:
            self.ssl_ctx = ssl._create_unverified_context()

    # -- authentication ----------------------------------------------------- #

    def authenticate(self):
        auth = self.cfg["auth"]
        mode = (auth.get("mode") or "password").lower()

        if mode == "private_token":
            if not auth.get("private_token"):
                die("auth.mode 是 private_token，但 auth.private_token 为空")
            self.auth_header = "Token " + auth["private_token"]
            return

        if mode == "access_key":
            # Signed per request, nothing to do up front.
            if not (auth.get("access_key_id") and auth.get("access_key_secret")):
                die("auth.mode 是 access_key，但 access_key_id / access_key_secret 为空")
            return

        if mode == "password":
            username, password = resolve_credentials(self.cfg)
            if not username:
                die("没有取到 JumpServer 用户名：请在配置里填 auth.username，"
                    "或在动态文件夹的 Credentials 页填写。")
            if not password:
                die(no_password_help())
            self.username = username
            body = json.dumps({
                "username": username,
                "password": password,
            }).encode("utf-8")
            payload = self._request("POST", "/api/v1/authentication/auth/", body=body)
            token = payload.get("token") if isinstance(payload, dict) else None
            if not token:
                die("登录成功但响应里没有 token: {0}".format(payload))
            self.token = token
            self.auth_header = "Bearer " + token
            return

        die("未知的 auth.mode: {0}（可选 password / private_token / access_key）".format(mode))

    # -- request plumbing --------------------------------------------------- #

    def _sign(self, method, path, accept, date):
        """HTTP Signature (hmac-sha256) over (request-target), accept, date."""
        auth = self.cfg["auth"]
        target = "{0} {1}".format(method.lower(), path)
        signing_string = "\n".join([
            "(request-target): " + target,
            "accept: " + accept,
            "date: " + date,
        ])
        digest = hmac.new(
            auth["access_key_secret"].encode("utf-8"),
            signing_string.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        signature = base64.b64encode(digest).decode("ascii")
        return (
            'Signature keyId="{0}",algorithm="hmac-sha256",'
            'headers="(request-target) accept date",signature="{1}"'
        ).format(auth["access_key_id"], signature)

    def _request(self, method, path, params=None, body=None):
        if params:
            path = path + "?" + urllib.parse.urlencode(params, doseq=True)
        url = self.base + path
        accept = "application/json"

        headers = {"Accept": accept, "Content-Type": "application/json"}
        if self.cfg.get("org_id"):
            headers["X-JMS-ORG"] = self.cfg["org_id"]

        if self.auth_header:
            headers["Authorization"] = self.auth_header
        elif (self.cfg["auth"].get("mode") or "").lower() == "access_key":
            date = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S GMT")
            headers["Date"] = date
            headers["Authorization"] = self._sign(method, path, accept, date)

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout,
                                        context=self.ssl_ctx) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:600]
            if exc.code == 401:
                die("认证失败 (401): {0}".format(detail))
            die("{0} {1} 返回 HTTP {2}: {3}".format(method, path, exc.code, detail))
        except urllib.error.URLError as exc:
            die("连接 {0} 失败: {1}".format(url, exc.reason))
        except OSError as exc:
            die("连接 {0} 失败: {1}".format(url, exc))

        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            die("{0} 返回的不是 JSON: {1}".format(path, raw[:300]))

    def get(self, path, **params):
        return self._request("GET", path, params=params or None)

    def paginate(self, path, **params):
        """Follow DRF's `next` links and yield every result."""
        page = self.get(path, **params)
        while True:
            if isinstance(page, list):
                for item in page:
                    yield item
                return
            for item in page.get("results", []):
                yield item
            nxt = page.get("next")
            if not nxt:
                return
            parsed = urllib.parse.urlparse(nxt)
            query = urllib.parse.parse_qs(parsed.query)
            page = self.get(parsed.path, **{k: v for k, v in query.items()})


# --------------------------------------------------------------------------- #
# data collection
# --------------------------------------------------------------------------- #

def fetch_assets(client):
    return list(client.paginate("/api/v1/perms/users/self/assets/"))


def fetch_nodes(client):
    """id -> node full path, e.g. '/生产环境/k8s'."""
    mapping = {}
    for node in client.paginate("/api/v1/perms/users/self/nodes/"):
        path = node.get("full_value") or node.get("value") or node.get("name") or ""
        mapping[node.get("id")] = path.strip("/")
    return mapping


def fetch_asset_detail(client, asset_id):
    return client.get("/api/v1/perms/users/self/assets/{0}/".format(asset_id))


def collect(cfg, client):
    """Return [(asset, accounts), ...] for every asset the user may reach."""
    assets = fetch_assets(client)
    if not assets:
        return [], {}

    nodes = {}
    if cfg["options"].get("group_by") == "node":
        nodes = fetch_nodes(client)

    workers = max(1, int(cfg["options"].get("workers") or 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        details = list(pool.map(
            lambda a: fetch_asset_detail(client, a["id"]), assets))

    pairs = []
    for asset, detail in zip(assets, details):
        accounts = (detail or {}).get("permed_accounts") or []
        pairs.append((asset, accounts))
    return pairs, nodes


# --------------------------------------------------------------------------- #
# RoyalJSON construction
# --------------------------------------------------------------------------- #

def pick_target(asset, use_uuid):
    """koko resolves the asset target as a UUID or as an IP address."""
    if use_uuid:
        return asset["id"]
    address = (asset.get("address") or "").strip()
    if address:
        return address
    return asset["id"]


def make_login_username(jms_user, account, target, preferred="auto"):
    """Build koko's direct-login username: user SEP account SEP target.

    koko tries '@' first, then '#', so '@' is used unless one of the three
    parts already contains it (AD-style account names, typically).
    """
    parts = (jms_user, account, target)
    candidates = ["@", "#"] if preferred in (None, "", "auto") else [preferred]
    for sep in candidates:
        if not any(sep in part for part in parts):
            return sep.join(parts)
    raise ValueError("用户名/账号/资产里同时含有 @ 和 #，无法拼出登录名")


def apply_connection_auth(connection, cfg, password):
    """Decide how a generated SSH connection authenticates to koko."""
    spec = cfg.get("connection_auth") or {}
    mode = (spec.get("mode") or "password").lower()

    if mode == "password":
        if password:
            connection["Password"] = password
        return

    if mode == "none":
        return

    if mode == "key":
        key_file = os.path.expanduser((spec.get("key_file") or "").strip())
        if not key_file:
            die("connection_auth.mode 是 key，但 connection_auth.key_file 没填")
        connection["KeyFilePath"] = key_file
        if spec.get("key_passphrase"):
            connection["KeyFilePassphrase"] = spec["key_passphrase"]
        return

    die("未知的 connection_auth.mode: {0}（可选 password / key / none）".format(mode))


def safe_folder_name(text):
    """RoyalJSON uses '/' as the folder separator, so keep it out of names."""
    return text.replace("/", "-").replace("\\", "-").strip()


def apply_terminal_settings(connection, cfg):
    """Keep the terminal content so it can still be scrolled back after the
    session ends -- whether by `exit` or by a dropped link."""
    terminal = cfg.get("terminal") or {}
    properties = {}
    if terminal.get("keep_content_on_disconnect", True):
        properties["KeepTerminalContentOnDisconnect"] = True
    if terminal.get("show_history_after_disconnect", True):
        properties["ShowHistoryAfterDisconnect"] = True
    if properties:
        connection["Properties"] = properties


def account_display_names(entries):
    """Name each connection after the account's username.

    JumpServer hands accounts an `alias`, which for accounts discovered on an
    asset is often just a UUID -- useless in the tree, since what people
    recognise is the username. When two accounts on the same asset genuinely
    share a username, the alias is appended to tell them apart.
    """
    usernames = [account_user for _, account_user in entries]
    duplicated = {u for u in usernames if usernames.count(u) > 1}

    names = []
    for account, account_user in entries:
        name = account_user
        if account_user in duplicated:
            alias = (account.get("alias") or account.get("name") or "").strip()
            if alias and not looks_like_uuid(alias) and alias != account_user:
                name = "{0} ({1})".format(account_user, alias)
        names.append(name)
    return names


def labelled(value):
    """JumpServer serialises choice fields as {"value": ..., "label": ...}."""
    if isinstance(value, dict):
        return str(value.get("label") or value.get("value") or "").strip()
    return value.strip() if isinstance(value, str) else ""


def platform_name(asset):
    platform = asset.get("platform")
    if isinstance(platform, dict):
        return str(platform.get("name") or "").strip()
    return platform.strip() if isinstance(platform, str) else ""


def asset_summary(asset):
    """The one-liner for the object list's Description column."""
    parts = [platform_name(asset), (asset.get("org_name") or "").strip()]
    summary = " · ".join(p for p in parts if p)
    return summary or labelled(asset.get("category")) or (asset.get("address") or "")


def asset_notes(asset, account_names):
    """The detail pane, so a row says more than just its name."""
    rows = [
        ("名称", asset.get("name")),
        ("地址", asset.get("address")),
        ("平台", platform_name(asset)),
        ("类型", labelled(asset.get("type"))),
        ("组织", asset.get("org_name")),
        ("备注", asset.get("comment")),
        ("可用账号", "、".join(account_names)),
        ("资产 ID", asset.get("id")),
    ]
    body = "".join(
        '<tr><th align="left">{0}</th><td>{1}</td></tr>'.format(
            html.escape(str(label)), html.escape(str(value)))
        for label, value in rows if value
    )
    return "<h3>{0}</h3><table>{1}</table>".format(
        html.escape(str(asset.get("name") or "")), body)


class FolderTree:
    """Builds nested Folder objects.

    rJSON's `Path` shorthand cannot set properties on the folders themselves,
    so anything that should show up on an asset row -- a description, notes --
    requires making the folders first-class objects and nesting into them.
    """

    def __init__(self):
        self.roots = []
        self._by_path = {}

    def ensure(self, parts):
        """Return (leaf folder, its Objects list), creating the chain."""
        listing = self.roots
        key = ""
        node = None
        for part in parts:
            key = (key + "/" + part) if key else part
            node = self._by_path.get(key)
            if node is None:
                node = {"Type": "Folder", "Name": part, "Objects": []}
                self._by_path[key] = node
                listing.append(node)
            listing = node["Objects"]
        return node, listing


def build_objects(cfg, pairs, nodes):
    options = cfg["options"]
    jms_user, password = resolve_credentials(cfg)
    jms_user = jms_user or "unknown"

    group_by = options.get("group_by") or "asset"
    prefix = [p for p in (options.get("path_prefix") or "").split("/") if p]
    skip_manual = options.get("skip_manual_accounts", True)
    use_uuid = options.get("use_uuid_as_target", False)

    tree = FolderTree()
    skipped = 0

    for asset, accounts in pairs:
        name = asset.get("name") or asset.get("address") or asset["id"]
        address = (asset.get("address") or "").strip()
        title = "{0} ({1})".format(name, address) if address and address != name else name
        title = safe_folder_name(title)

        parts = list(prefix)
        if group_by == "node" and asset.get("nodes"):
            node_path = nodes.get(asset["nodes"][0], "")
            parts += [p for p in node_path.split("/") if p]
        parts.append(title)

        target = pick_target(asset, use_uuid)

        if not options.get("include_accounts", True):
            accounts = [{"username": jms_user}]

        usable = []
        for account in accounts or []:
            account_user = (account.get("username") or "").strip()
            if not account_user or account_user.startswith("@"):
                # "@INPUT" is JumpServer's "type the password by hand" pseudo
                # account; it cannot be expressed in the direct-login format.
                if skip_manual:
                    continue
                account_user = account_user.lstrip("@")
            if account_user:
                usable.append((account, account_user))

        if not usable:
            skipped += 1
            continue

        names = account_display_names(usable)
        folder, listing = tree.ensure(parts)
        folder["Description"] = asset_summary(asset)
        folder["Notes"] = asset_notes(asset, names)

        for (account, account_user), display in zip(usable, names):
            try:
                username = make_login_username(jms_user, account_user, target,
                                               options.get("separator", "auto"))
            except ValueError as exc:
                log("跳过 {0} / {1}: {2}".format(title, account_user, exc))
                continue

            connection = {
                "Type": "TerminalConnection",
                "TerminalConnectionType": "SSH",
                "Name": display,
                "ComputerName": cfg["koko"]["host"],
                "Port": int(cfg["koko"]["port"]),
                "Username": username,
                "CredentialsFromParent": False,
                "Description": asset_summary(asset),
            }
            apply_connection_auth(connection, cfg, password)
            apply_terminal_settings(connection, cfg)
            listing.append(connection)

    if skipped:
        log("注意: {0} 个资产没有任何可用账号，已跳过".format(skipped))
    return tree.roots


def count_connections(objects):
    total = 0
    for obj in objects:
        if obj.get("Type") == "Folder":
            total += count_connections(obj.get("Objects") or [])
        else:
            total += 1
    return total


def emit(cfg, client):
    pairs, nodes = collect(cfg, client)
    objects = build_objects(cfg, pairs, nodes)
    document = {
        "Objects": objects,
    }
    json.dump(document, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    log("已生成 {0} 个连接".format(count_connections(objects)))


# --------------------------------------------------------------------------- #
# the .rdfe import file
# --------------------------------------------------------------------------- #

# Royal TSX performs $token$ substitution on this text and prepends the
# interpreter's shebang, so the file itself must contain no '$' and no '#!'
# outside the two tokens below.
#
# The credential is fed to Python on stdin through a heredoc with a *quoted*
# delimiter, which bash passes through byte for byte. That is what makes it
# safe to substitute a password inline: quotes, '$' or backticks in the
# password cannot break the script the way `VAR='$Token$'` would.
BOOTSTRAP = (
    "# 真正的逻辑在下面这个 Python 文件里，方便在终端直接跑和调试。\n"
    "# 下面两行会被 Royal TSX 替换成 Credentials 页里填的用户名和密码，\n"
    "# 通过引号包住的 heredoc 传进去，所以密码里有引号或特殊字符也没问题。\n"
    "env JMS_CREDS_ON_STDIN=1 python3 " + SCRIPT_PATH
    + " <<'JMS_CREDS_EOF'\n"
    "$EffectiveUsername$\n"
    "$EffectivePassword$\n"
    "JMS_CREDS_EOF\n"
)

NOTES = """<h2><strong>JumpServer</strong></h2>
<p>把 JumpServer 上有权限访问的资产和账号列成一棵可点击的树，点一下就直接
SSH 到目标机器，不再需要在堡垒机的 TUI 菜单里翻找。</p>

<h3>第一次使用</h3>
<ol>
<li>选中这个动态文件夹，打开右边的 <strong>Credentials</strong> 页。</li>
<li>选择 <strong>Specify username and password</strong>，填上你的 JumpServer
    用户名和密码。密码会加密保存在文档里。</li>
<li>右键文件夹，选 <strong>Reload</strong>。</li>
</ol>
<p>之后展开任意一台机器，点下面的账号即可连接。</p>

<h3>说明</h3>
<ul>
<li>SSH 目标是 koko（JumpServer 的 SSH 服务）：<code>{ssh_target}</code>。</li>
<li>用户名按 koko 的直连格式拼成
    <code>用户名@账号@资产IP</code>，由脚本自动生成。</li>
<li>本机需要 Python 3（macOS 自带 <code>/usr/bin/python3</code>）。</li>
<li>配置文件 <code>~/.config/royaltsx-jumpserver/config.json</code>
    只在需要改分组方式等选项时才动。</li>
</ul>
"""


def try_load_config():
    """Like load_config, but returns None instead of dying."""
    try:
        if not os.path.exists(CONFIG_PATH):
            return None
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return deep_merge(DEFAULT_CONFIG, json.load(handle))
    except (ValueError, OSError):
        return None


def emit_rdfe(path):
    cfg = try_load_config() or DEFAULT_CONFIG
    username = (cfg.get("auth") or {}).get("username") or ""
    koko = cfg.get("koko") or {}
    ssh_target = "{0}:{1}".format(koko.get("host") or "?", koko.get("port") or "?")

    document = {
        "Name": "Dynamic Folder Export",
        "Objects": [
            {
                "Type": "DynamicFolder",
                "Name": "JumpServer",
                "Description": "JumpServer 资产与账号，点击直接连接。",
                "Notes": NOTES.format(ssh_target=ssh_target),
                "CustomProperties": [
                    {"Name": "配置", "Type": "Header", "Value": ""},
                    {"Name": "配置文件路径", "Type": "Text", "Value": CONFIG_PATH},
                    {"Name": "koko SSH", "Type": "Text", "Value": ssh_target},
                ],
                # Interpreter IDs are the bare lowercase names Royal TSX lists
                # under DynFolder<Name>Path settings: bash, perl, php,
                # powershell, python, ruby, json, javascript.
                "ScriptInterpreter": "bash",
                "DynamicCredentialScriptInterpreter": "json",
                "DynamicCredentialScript": "{\n\t\"Username\": \"user\",\n\t\"Password\": \"pass\"\n}",
                "Script": BOOTSTRAP,
                # Credential settings, so the password is typed once in the GUI
                # and stays encrypted in the document instead of sitting in a
                # file on disk. The default token mode (inline substitution) is
                # left alone: the bootstrap consumes the substituted values via
                # a quoted heredoc, so there is no reason to switch the folder
                # to Environment Variables mode.
                "Properties": {
                    "CredentialMode": 2,
                    "CredentialFromParent": False,
                    "CredentialAutologon": True,
                    "CredentialUsername": username,
                },
            }
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=4)
        handle.write("\n")
    log("已写出 {0}".format(path))


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #

# koko listens on 2222 out of the box, but plenty of deployments move it. These
# are the ports worth trying when the configured one does not answer.
CANDIDATE_SSH_PORTS = [2222, 22222, 22228, 2223, 2224, 2022, 2200, 8022,
                       10022, 22022, 3022, 22]


def probe_ssh(host, port, timeout=5):
    """Return the SSH banner, or '' if nothing answered."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return sock.recv(64).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def discover_ssh_ports(host, timeout=2):
    ports = [p for p in CANDIDATE_SSH_PORTS]
    found = []

    def check(port):
        banner = probe_ssh(host, port, timeout)
        return (port, banner) if banner.startswith("SSH-") else None

    with ThreadPoolExecutor(max_workers=len(ports)) as pool:
        for result in pool.map(check, ports):
            if result:
                found.append(result)
    return sorted(found)


def run_check(cfg, client):
    """Human-readable sanity check, run from a terminal."""
    def say(line=""):
        print(line, flush=True)

    say("配置文件   : {0}".format(CONFIG_PATH))
    say("JumpServer : {0}".format(cfg["base_url"]))
    say("koko SSH   : {0}:{1}".format(cfg["koko"]["host"], cfg["koko"]["port"]))
    say("认证方式   : {0}".format(cfg["auth"].get("mode")))

    env_username, env_password = credentials_from_royaltsx()
    source = (cfg.get("password_source") or "royaltsx").lower()
    if env_password:
        say("凭据来源   : Royal TSX 动态文件夹（用户名 {0}）".format(env_username or "-"))
    else:
        say("凭据来源   : {0}".format(source))
        if source in ("royaltsx", "auto"):
            say("             注意：Royal TSX 只在跑脚本时把凭据传进来，")
            say("             在终端里跑看不到。如果密码只填在文件夹的")
            say("             Credentials 页，这里会认证失败——那是正常的，")
            say("             以 Royal TSX 里 Reload 的结果为准。")
    say()

    client.authenticate()
    say("[ok] 认证成功")

    who = client.get("/api/v1/users/profile/")
    say("[ok] 当前用户: {0} ({1})".format(
        who.get("name") or who.get("username"), who.get("id")))

    assets = fetch_assets(client)
    say("[ok] 有权限的资产: {0} 个".format(len(assets)))

    if assets:
        detail = fetch_asset_detail(client, assets[0]["id"])
        accounts = detail.get("permed_accounts") or []
        say("[ok] 样例资产 {0} 的账号: {1}".format(
            assets[0].get("name"), [a.get("username") for a in accounts]))

    host = cfg["koko"]["host"]
    port = int(cfg["koko"]["port"])
    banner = probe_ssh(host, port)
    if banner:
        say("[ok] koko SSH {0}:{1} 可连接 -- {2}".format(host, port, banner))
    else:
        say("[!!] koko SSH {0}:{1} 连不上".format(host, port))
        found = discover_ssh_ports(host)
        if found:
            say("     这些端口在跑 SSH 服务，koko 很可能在其中：")
            for candidate, text in found:
                say("       {0:<6} {1}".format(candidate, text))
            say("     把配置里的 koko.port 改成上面那个。")
        else:
            say("     常见端口都试过了，当前网络到堡垒机的 SSH 端口不通。")
            say("     确认一下平时 SSH 堡垒机走的是哪个地址和端口，或者是否需要先连 VPN。")
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="把 JumpServer 的资产渲染成 Royal TSX 动态文件夹。")
    parser.add_argument("--check", action="store_true",
                        help="打印诊断信息（在终端里运行）")
    parser.add_argument("--emit-rdfe", metavar="FILE",
                        help="生成可导入 Royal TSX 的 .rdfe 文件")
    parser.add_argument("--raw", metavar="PATH",
                        help="直接打印某个 API 路径的原始返回，便于排查")
    args = parser.parse_args(argv)

    if args.emit_rdfe:
        emit_rdfe(args.emit_rdfe)
        return 0

    cfg = load_config()
    client = JmsClient(cfg)

    if args.raw:
        client.authenticate()
        json.dump(client.get(args.raw), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.check:
        return run_check(cfg, client)

    client.authenticate()
    emit(cfg, client)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
