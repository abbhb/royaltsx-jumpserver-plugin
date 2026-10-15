#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks the RoyalJSON we build, using JumpServer's real response shapes.

Run with:  python3 tests/test_royaljson.py
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import jms_royaltsx as jms  # noqa: E402


class FakeStdin:
    """Stands in for sys.stdin so tests never block on a real pipe."""

    def __init__(self, data="", tty=False):
        self._data = data
        self._tty = tty

    def isatty(self):
        return self._tty

    def read(self):
        return self._data


class StdinIsolated(unittest.TestCase):
    """Base class that keeps the credential lookup away from the real stdin."""

    def setUp(self):
        self._real_stdin = sys.stdin
        self._env_backup = dict(os.environ)
        sys.stdin = FakeStdin(tty=True)
        jms._STDIN_CREDENTIALS = None
        os.environ.pop("JMS_CREDS_ON_STDIN", None)
        for key in list(os.environ):
            flat = "".join(c for c in key.lower() if c.isalpha())
            if flat.endswith(("effectivepassword", "effectiveusername")):
                del os.environ[key]

    def tearDown(self):
        sys.stdin = self._real_stdin
        jms._STDIN_CREDENTIALS = None
        os.environ.clear()
        os.environ.update(self._env_backup)

    def feed_stdin(self, data):
        """Pretend the bootstrap set its marker and substituted the tokens."""
        sys.stdin = FakeStdin(data)
        os.environ["JMS_CREDS_ON_STDIN"] = "1"
        jms._STDIN_CREDENTIALS = None


def base_config(**overrides):
    cfg = jms.deep_merge(jms.DEFAULT_CONFIG, {
        "base_url": "https://jumpserver.example.com",
        "koko": {"host": "jumpserver.example.com", "port": 2222},
        "auth": {"mode": "password", "username": "alice", "password": "s3cret"},
        "password_source": "config",
    })
    return jms.deep_merge(cfg, overrides)


# Shapes taken from apps/perms/serializers/user_permission.py
ASSET_MASTER = {
    "id": "b1e0d0a2-0000-4000-8000-000000000001",
    "name": "k8s-master01",
    "address": "10.0.0.10",
    "nodes": ["n-1"],
    "platform": {"id": 1, "name": "Linux"},
    "category": {"value": "host", "label": "主机"},
    "type": {"value": "linux", "label": "Linux"},
    "comment": "",
    "is_active": True,
    "org_id": "00000000-0000-0000-0000-000000000002",
    "org_name": "Default Org",
}

ASSET_WEB = {
    "id": "b1e0d0a2-0000-4000-8000-000000000002",
    "name": "web-01",
    "address": "web-01.internal",  # not an IP: koko needs the UUID here
    "nodes": [],
    "platform": {"id": 1, "name": "Linux"},
    "category": {"value": "host", "label": "主机"},
    "type": {"value": "linux", "label": "Linux"},
    "comment": "",
    "is_active": True,
    "org_id": "00000000-0000-0000-0000-000000000002",
    "org_name": "Default Org",
}

# JumpServer hands accounts an `alias` that is often just a UUID for accounts
# discovered on the asset -- which is what showed up in the tree as
# "403bef6d-c362-4582-8..." before. The username is the recognisable one.
DETAIL_MASTER = dict(ASSET_MASTER, permed_accounts=[
    {"id": "a-1", "alias": "403bef6d-c362-4582-8ac7-0e6bd2a4b4c1",
     "name": "403bef6d-c362-4582-8ac7-0e6bd2a4b4c1", "username": "root",
     "has_username": True, "has_secret": True, "secret_type": "password",
     "actions": {}, "date_expired": None},
    {"id": "a-2", "alias": "手动输入", "name": "手动输入", "username": "@INPUT",
     "has_username": False, "has_secret": False, "secret_type": "password",
     "actions": {}, "date_expired": None},
    {"id": "a-3", "alias": "00a1b2c3-d4e5-4678-9abc-def012345678",
     "name": "00a1b2c3-d4e5-4678-9abc-def012345678", "username": "deploy",
     "has_username": True, "has_secret": True, "secret_type": "password",
     "actions": {}, "date_expired": None},
])

DETAIL_WEB = dict(ASSET_WEB, permed_accounts=[
    {"id": "a-4", "alias": "63e020fa-f0ad-4612-8bf2-1d2e3f4a5b6c",
     "name": "63e020fa-f0ad-4612-8bf2-1d2e3f4a5b6c", "username": "root",
     "has_username": True, "has_secret": True, "secret_type": "password",
     "actions": {}, "date_expired": None},
])


class FakeClient:
    """Stands in for JmsClient, replaying JumpServer's own payload shapes."""

    def __init__(self, assets, details, nodes=()):
        self.assets = assets
        self.details = details
        self.nodes = list(nodes)

    def paginate(self, path, **params):
        if path.endswith("/nodes/"):
            yield from self.nodes
        else:
            yield from self.assets

    def get(self, path, **params):
        for asset_id, detail in self.details.items():
            if asset_id in path:
                return detail
        raise AssertionError("unexpected path " + path)


def walk(objects, prefix=()):
    """Yield (folder path, object) for everything in the generated tree."""
    for obj in objects:
        if obj.get("Type") == "Folder":
            yield from walk(obj.get("Objects") or [], prefix + (obj["Name"],))
        else:
            yield prefix, obj


def build(cfg, client):
    """Every (folder path, connection) pair the generator produces."""
    pairs, nodes = jms.collect(cfg, client)
    return list(walk(jms.build_objects(cfg, pairs, nodes)))


def connections(cfg, client):
    return [obj for _, obj in build(cfg, client)]


class TestUsernameFormat(unittest.TestCase):
    def test_uses_at_when_unambiguous(self):
        self.assertEqual(
            jms.make_login_username("alice", "root", "10.0.0.10"),
            "alice@root@10.0.0.10")

    def test_falls_back_to_hash_for_ad_style_accounts(self):
        # An account like DOMAIN\user is fine, but user@domain would make the
        # '@' form ambiguous, so koko's second separator has to be used.
        self.assertEqual(
            jms.make_login_username("alice", "bob@corp.local", "10.0.0.10"),
            "alice#bob@corp.local#10.0.0.10")

    def test_rejects_when_both_separators_present(self):
        with self.assertRaises(ValueError):
            jms.make_login_username("alice", "a@b#c", "10.0.0.10")

    def test_folder_name_keeps_path_separators_out(self):
        self.assertEqual(jms.safe_folder_name("a/b\\c"), "a-b-c")


class TestAccountNaming(unittest.TestCase):
    def test_plain_username_is_used(self):
        entries = [({"alias": "ignored-uuid"}, "root")]
        self.assertEqual(jms.account_display_names(entries), ["root"])

    def test_duplicate_usernames_get_the_alias_appended(self):
        entries = [
            ({"alias": "系统账号"}, "root"),
            ({"alias": "特权账号"}, "root"),
        ]
        self.assertEqual(jms.account_display_names(entries),
                         ["root (系统账号)", "root (特权账号)"])

    def test_duplicate_usernames_with_uuid_aliases_stay_bare(self):
        # Nothing useful to append, so a duplicate name beats a UUID.
        entries = [
            ({"alias": "11111111-2222-4333-8444-555555555555"}, "root"),
            ({"alias": "66666666-7777-4888-8999-000000000000"}, "root"),
        ]
        self.assertEqual(jms.account_display_names(entries), ["root", "root"])

    def test_uuid_detection(self):
        self.assertTrue(jms.looks_like_uuid("403bef6d-c362-4582-8ac7-0e6bd2a4b4c1"))
        self.assertFalse(jms.looks_like_uuid("root"))
        self.assertFalse(jms.looks_like_uuid(""))
        self.assertFalse(jms.looks_like_uuid(None))


class TestBuildObjects(StdinIsolated):
    def setUp(self):
        super().setUp()
        self.client = FakeClient(
            [ASSET_MASTER, ASSET_WEB],
            {ASSET_MASTER["id"]: DETAIL_MASTER, ASSET_WEB["id"]: DETAIL_WEB},
            nodes=[{"id": "n-1", "name": "k8s", "value": "/prod/k8s",
                    "full_value": "/prod/k8s", "key": "k8s"}],
        )

    def test_one_connection_per_account(self):
        # master has root + deploy (the @INPUT pseudo account is skipped),
        # web has root.
        self.assertEqual(len(connections(base_config(), self.client)), 3)

    def test_connection_shape(self):
        found = [(path, obj) for path, obj in build(base_config(), self.client)
                 if obj["Name"] == "root" and "master" in path[-1]]
        self.assertEqual(len(found), 1)
        path, root = found[0]
        self.assertEqual(root["Type"], "TerminalConnection")
        self.assertEqual(root["TerminalConnectionType"], "SSH")
        self.assertEqual(root["ComputerName"], "jumpserver.example.com")
        self.assertEqual(root["Port"], 2222)
        self.assertEqual(root["Username"], "alice@root@10.0.0.10")
        self.assertEqual(root["Password"], "s3cret")
        self.assertEqual(path[-1], "k8s-master01 (10.0.0.10)")

    def test_assets_are_folders_carrying_a_description(self):
        # The object list's Description column is populated from the folder, so
        # a row says more than just the asset's name.
        pairs, nodes = jms.collect(base_config(), self.client)
        roots = jms.build_objects(base_config(), pairs, nodes)
        self.assertEqual([f["Name"] for f in roots],
                         ["k8s-master01 (10.0.0.10)", "web-01 (web-01.internal)"])
        for folder in roots:
            self.assertEqual(folder["Type"], "Folder")
            self.assertTrue(folder["Description"])
            self.assertTrue(folder["Notes"])

    def test_folder_description_is_platform_and_org(self):
        pairs, nodes = jms.collect(base_config(), self.client)
        roots = jms.build_objects(base_config(), pairs, nodes)
        self.assertEqual(roots[0]["Description"], "Linux · Default Org")

    def test_folder_notes_list_the_available_accounts(self):
        pairs, nodes = jms.collect(base_config(), self.client)
        roots = jms.build_objects(base_config(), pairs, nodes)
        self.assertIn("root", roots[0]["Notes"])
        self.assertIn("deploy", roots[0]["Notes"])

    def test_folder_notes_escape_html_in_asset_fields(self):
        asset = dict(ASSET_MASTER, comment="<script>alert(1)</script>")
        client = FakeClient([asset], {asset["id"]: DETAIL_MASTER})
        pairs, nodes = jms.collect(base_config(), client)
        roots = jms.build_objects(base_config(), pairs, nodes)
        self.assertNotIn("<script>", roots[0]["Notes"])
        self.assertIn("&lt;script&gt;", roots[0]["Notes"])

    def test_manual_input_account_skipped(self):
        self.assertFalse([o for o in connections(base_config(), self.client)
                          if "@INPUT" in o["Username"]])

    def test_connections_are_named_after_the_account_username(self):
        names = sorted(obj["Name"] for path, obj in build(base_config(), self.client)
                       if "master" in path[-1])
        self.assertEqual(names, ["deploy", "root"])

    def test_uuid_aliases_never_reach_the_tree(self):
        for obj in connections(base_config(), self.client):
            self.assertFalse(jms.looks_like_uuid(obj["Name"]), obj["Name"])

    def test_terminal_content_is_kept_after_disconnect(self):
        for obj in connections(base_config(), self.client):
            self.assertTrue(obj["Properties"]["KeepTerminalContentOnDisconnect"])
            self.assertTrue(obj["Properties"]["ShowHistoryAfterDisconnect"])

    def test_terminal_settings_can_be_turned_off(self):
        cfg = base_config(terminal={"keep_content_on_disconnect": False,
                                    "show_history_after_disconnect": False})
        objs = connections(cfg, self.client)
        self.assertTrue(objs)
        self.assertTrue(all("Properties" not in o for o in objs))

    def test_scrollback_limit_is_left_alone_by_default(self):
        # Royal TSX caps this at 1000 lines; only override when asked, so the
        # plugin does not quietly change everyone's memory footprint.
        for obj in connections(base_config(), self.client):
            self.assertNotIn("HistoryMaxLength", obj["Properties"])
            self.assertNotIn("HistoryUnlimited", obj["Properties"])

    def test_scrollback_limit_can_be_raised(self):
        cfg = base_config(terminal={"history_max_length": 50000})
        for obj in connections(cfg, self.client):
            self.assertEqual(obj["Properties"]["HistoryMaxLength"], 50000)

    def test_scrollback_can_be_unlimited(self):
        cfg = base_config(terminal={"history_unlimited": True})
        for obj in connections(cfg, self.client):
            self.assertTrue(obj["Properties"]["HistoryUnlimited"])

    def test_non_ip_address_uses_uuid(self):
        # koko's GetUserPermAssetsByIP only matches addresses; a hostname would
        # never resolve, so the asset UUID is the safe target.
        cfg = base_config(options={"use_uuid_as_target": True})
        web = [obj for path, obj in build(cfg, self.client)
               if "web-01" in path[-1]]
        self.assertEqual(web[0]["Username"], "alice@root@" + ASSET_WEB["id"])

    def test_group_by_node_nests_under_the_node_path(self):
        cfg = base_config(options={"group_by": "node"})
        paths = [path for path, obj in build(cfg, self.client)
                 if "master" in path[-1]]
        self.assertEqual(paths[0], ("prod", "k8s", "k8s-master01 (10.0.0.10)"))

    def test_password_can_be_left_out_of_connections(self):
        # With connection_auth "none" the password stays in the folder's
        # credential and never reaches the generated connections.
        cfg = base_config(connection_auth={"mode": "none"})
        objs = connections(cfg, self.client)
        self.assertTrue(objs)
        self.assertTrue(all("Password" not in o for o in objs))

    def test_connections_can_use_an_ssh_key(self):
        cfg = base_config(connection_auth={
            "mode": "key", "key_file": "~/.ssh/id_rsa"})
        objs = connections(cfg, self.client)
        self.assertTrue(objs)
        for obj in objs:
            self.assertEqual(obj["KeyFilePath"], os.path.expanduser("~/.ssh/id_rsa"))
            self.assertNotIn("Password", obj)

    def test_key_mode_without_a_path_is_an_error(self):
        cfg = base_config(connection_auth={"mode": "key"})
        with self.assertRaises(SystemExit):
            build(cfg, self.client)

    def test_keychain_password_is_read_from_security(self):
        cfg = base_config(password_source="keychain")
        cfg["auth"]["password"] = ""

        recorded = {}

        class Done:
            returncode = 0
            stdout = "from-keychain\n"

        def fake_run(cmd, **kwargs):
            recorded["cmd"] = cmd
            return Done()

        real_run = jms.subprocess.run
        jms.subprocess.run = fake_run
        try:
            password = jms.resolve_password(cfg)
        finally:
            jms.subprocess.run = real_run

        self.assertEqual(password, "from-keychain")
        self.assertIn("find-generic-password", recorded["cmd"])
        self.assertIn("alice", recorded["cmd"])


class TestCredentials(StdinIsolated):
    """The password should come from Royal TSX's GUI, not from a file."""

    def test_reads_the_dynamic_folders_own_credential(self):
        os.environ["DynFolder_EffectiveUsername"] = "alice"
        os.environ["DynFolder_EffectivePassword"] = "pw with 'quotes' & $dollars"
        self.assertEqual(jms.credentials_from_royaltsx(),
                         ("alice", "pw with 'quotes' & $dollars"))

    def test_prefix_is_not_hardcoded(self):
        # The prefix is configurable in the folder's Advanced settings.
        os.environ["Whatever_EffectivePassword"] = "pw"
        self.assertEqual(jms.credentials_from_royaltsx()[1], "pw")

    def test_unrelated_variables_are_ignored(self):
        os.environ["SOME_PASSWORD"] = "nope"
        self.assertEqual(jms.credentials_from_royaltsx(), (None, None))

    def test_debug_hint_names_variables_but_never_values(self):
        os.environ["DynFolder_EffectivePassword"] = "super-secret"
        hint = jms.credential_debug_hint()
        self.assertIn("DynFolder_EffectivePassword", hint)
        self.assertNotIn("super-secret", hint)

    def test_gui_credential_beats_the_config_file(self):
        os.environ["DynFolder_EffectiveUsername"] = "gui-user"
        os.environ["DynFolder_EffectivePassword"] = "gui-pw"
        cfg = base_config(password_source="config")
        cfg["auth"]["password"] = "config-pw"
        self.assertEqual(jms.resolve_credentials(cfg), ("gui-user", "gui-pw"))

    def test_royaltsx_source_does_not_reach_for_the_keychain(self):
        # With nothing set in the GUI we must report that plainly rather than
        # silently falling back to a file the user never configured.
        cfg = base_config(password_source="royaltsx")
        username, password = jms.resolve_credentials(cfg)
        self.assertEqual(username, "alice")
        self.assertEqual(password, "")

    def test_auto_source_falls_back_to_the_config(self):
        cfg = base_config(password_source="auto")
        self.assertEqual(jms.resolve_credentials(cfg), ("alice", "s3cret"))

    def test_password_with_quotes_survives_into_connections(self):
        os.environ["DynFolder_EffectiveUsername"] = "alice"
        os.environ["DynFolder_EffectivePassword"] = "it's \"fine\" $HOME `id`"
        cfg = base_config(password_source="royaltsx")
        client = FakeClient(
            [ASSET_MASTER], {ASSET_MASTER["id"]: DETAIL_MASTER})
        objs = connections(cfg, client)
        self.assertEqual(objs[0]["Password"], "it's \"fine\" $HOME `id`")


class TestStdinCredentials(StdinIsolated):
    """The default path: Royal TSX substitutes the tokens into the script text
    and the bootstrap hands the resulting values over on stdin."""

    def test_reads_username_and_password(self):
        self.feed_stdin("alice\nhunter2\n")
        self.assertEqual(jms.credentials_from_stdin(), ("alice", "hunter2"))

    def test_password_may_contain_anything_bash_would_mangle(self):
        nasty = "it's \"fine\" $HOME `id` \\ ; rm -rf /"
        self.feed_stdin("alice\n" + nasty + "\n")
        self.assertEqual(jms.credentials_from_stdin()[1], nasty)

    def test_password_may_contain_newlines(self):
        self.feed_stdin("alice\nline1\nline2\n")
        self.assertEqual(jms.credentials_from_stdin()[1], "line1\nline2")

    def test_unsubstituted_tokens_are_not_mistaken_for_a_password(self):
        self.feed_stdin("$EffectiveUsername$\n$EffectivePassword$\n")
        self.assertEqual(jms.credentials_from_stdin(), (None, None))

    def test_empty_heredoc_yields_nothing(self):
        self.feed_stdin("\n\n")
        self.assertEqual(jms.credentials_from_stdin(), (None, None))

    def test_a_tty_is_never_read(self):
        # Running --check by hand must not block waiting for input.
        sys.stdin = FakeStdin("should not be read", tty=True)
        os.environ["JMS_CREDS_ON_STDIN"] = "1"
        jms._STDIN_CREDENTIALS = None
        self.assertEqual(jms.credentials_from_stdin(), (None, None))

    def test_stdin_is_ignored_without_the_bootstrap_marker(self):
        # Otherwise an inherited pipe nobody writes to would hang the script.
        sys.stdin = FakeStdin("alice\npw\n")
        os.environ.pop("JMS_CREDS_ON_STDIN", None)
        jms._STDIN_CREDENTIALS = None
        self.assertEqual(jms.credentials_from_stdin(), (None, None))

    def test_used_when_the_folder_has_no_env_vars(self):
        self.feed_stdin("alice\nfrom-heredoc\n")
        cfg = base_config(password_source="royaltsx")
        self.assertEqual(jms.resolve_credentials(cfg), ("alice", "from-heredoc"))

    def test_env_vars_win_over_stdin(self):
        os.environ["DynFolder_EffectivePassword"] = "from-env"
        self.feed_stdin("alice\nfrom-heredoc\n")
        cfg = base_config(password_source="royaltsx")
        self.assertEqual(jms.resolve_credentials(cfg)[1], "from-env")

    def test_stdin_is_only_consumed_once(self):
        # stdin can only be read once, so the result has to be cached.
        self.feed_stdin("alice\npw\n")
        cfg = base_config(password_source="royaltsx")
        self.assertEqual(jms.resolve_credentials(cfg)[1], "pw")
        self.assertEqual(jms.resolve_credentials(cfg)[1], "pw")

    def test_debug_hint_reports_a_missing_password(self):
        self.feed_stdin("alice\n\n")
        jms.credentials_from_stdin()      # populate the cache the hint reads
        self.assertIn("只收到用户名", jms.credential_debug_hint())

    def test_debug_hint_points_at_a_stale_bootstrap(self):
        # A folder imported from an older build sets no marker. Say that,
        # rather than blaming the tokens, which are perfectly fine.
        sys.stdin = FakeStdin("alice\npw\n")
        os.environ.pop("JMS_CREDS_ON_STDIN", None)
        jms._STDIN_CREDENTIALS = None
        jms.credentials_from_stdin()
        self.assertIn("重新导入", jms.credential_debug_hint())


class TestBootstrap(unittest.TestCase):
    """The generated script text is handed to Royal TSX verbatim."""

    def test_only_the_two_credential_tokens_use_a_dollar_sign(self):
        # Royal TSX substitutes every $token$ in this text, so a stray '$'
        # elsewhere would be misread as a token.
        remainder = jms.BOOTSTRAP
        for token in ("$EffectiveUsername$", "$EffectivePassword$"):
            self.assertIn(token, remainder)
            remainder = remainder.replace(token, "", 1)
        self.assertNotIn("$", remainder)

    def test_carries_no_shebang(self):
        # Royal TSX prepends the interpreter's own.
        self.assertNotIn("#!", jms.BOOTSTRAP)

    def test_heredoc_delimiter_is_quoted(self):
        # With an unquoted delimiter bash would expand and word-split whatever
        # the substituted password contains before Python ever saw it.
        self.assertIn("<<'JMS_CREDS_EOF'", jms.BOOTSTRAP)

    def test_heredoc_passes_shell_metacharacters_through_untouched(self):
        nasty = "it's \"fine\" $HOME `id` \\ ; echo pwned"
        script = ("python3 -c 'import sys;sys.stdout.write(sys.stdin.read())'"
                  " <<'JMS_CREDS_EOF'\nalice\n" + nasty + "\nJMS_CREDS_EOF\n")
        out = subprocess.run(["bash", "-c", script],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout, "alice\n" + nasty + "\n")

    def test_bootstrap_reaches_the_python_script(self):
        env = dict(os.environ, JMS_ROYALTSX_CONFIG="/nonexistent/config.json")
        out = subprocess.run(["bash", "-c", jms.BOOTSTRAP],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 1)
        self.assertIn("配置文件", out.stderr)


class TestConfigMerge(unittest.TestCase):
    def test_partial_config_keeps_defaults(self):
        cfg = jms.deep_merge(jms.DEFAULT_CONFIG, {"base_url": "https://x/"})
        self.assertEqual(cfg["options"]["workers"], 8)
        self.assertEqual(cfg["koko"]["port"], 2222)


if __name__ == "__main__":
    unittest.main(verbosity=2)
