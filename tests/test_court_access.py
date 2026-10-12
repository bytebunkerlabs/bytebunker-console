"""The court's access: folders and MCP servers a role may read, kept to the
harness's rules (a role holding data never has network), and sent to a
worker's harness the way it takes them."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import agents  # noqa: E402

ACCESS = {"folders": {"sales": "~/data/sales"}, "mcp_servers": ["pg"]}
MCP = {"pg": {"command": "npx", "args": ["-y", "pg-mcp"], "env": {"PGURL": "postgres://x"}, "enabled": True},
       "terminal": {"command": "python3", "args": ["mcp_terminal.py", "/w"], "env": {}},
       "my_server": {"command": "x", "args": []}}


class CourtAccess(unittest.TestCase):
    def test_a_data_role_keeps_its_grants(self):
        c = agents.clean_court({"roles": {"khazin": {"data": ["sales"], "mcp": ["pg__*"], "display": "khazin"}},
                                "access": ACCESS})
        self.assertEqual(c["roles"]["khazin"]["data"], ["sales"])
        self.assertEqual(c["access"], {"folders": {"sales": "~/data/sales"}, "mcp_servers": ["pg"]})

    def test_network_roles_never_hold_data(self):
        for role in ({"researcher": {"data": ["sales"]}}, {"analyst": {"data": ["sales"], "network": True}},
                     {"scout": {"base": "researcher", "mcp": ["pg__*"]}}):
            with self.assertRaisesRegex(ValueError, "cannot also hold data"):
                agents.clean_court({"roles": role, "access": ACCESS})
        # a role of your own from a network role, with its network turned off, may
        agents.clean_court({"roles": {"scout": {"base": "researcher", "network": False, "mcp": ["pg__*"]}},
                            "access": ACCESS})

    def test_grants_name_what_access_has(self):
        with self.assertRaisesRegex(ValueError, "not under Access"):
            agents.clean_court({"roles": {"khazin": {"data": ["hr"]}}, "access": ACCESS})
        with self.assertRaisesRegex(ValueError, "not a tool of a server shared"):
            agents.clean_court({"roles": {"khazin": {"mcp": ["web__*"]}}, "access": ACCESS})
        with self.assertRaisesRegex(ValueError, "full path"):
            agents.clean_court({"roles": {}, "access": {"folders": {"sales": "data/sales"}}})

    def test_the_run_gets_the_servers_settings_and_an_old_harness_gets_none(self):
        court = agents.clean_court({"roles": {"khazin": {"data": ["sales"], "mcp": ["pg__*"]},
                                              "researcher": {"display": "alim"}}, "access": ACCESS})
        new = agents.court_for_run(court, {"court": True, "access": True}, MCP)
        self.assertEqual(new["access"]["mcp_servers"]["pg"],
                         {"command": "npx", "args": ["-y", "pg-mcp"], "env": {"PGURL": "postgres://x"}})
        old = agents.court_for_run(court, {"court": True}, MCP)
        self.assertNotIn("access", old)
        self.assertNotIn("khazin", old["roles"])
        self.assertEqual(old["roles"]["researcher"], {"display": "alim"})
        self.assertEqual(court["access"]["mcp_servers"], ["pg"])         # the saved court is not changed

    def test_the_apps_own_tools_stay_here(self):
        names = {m["name"]: m for m in agents.shareable_mcp(MCP)}
        self.assertNotIn("terminal", names)
        self.assertTrue(names["pg"]["ok_name"])
        self.assertFalse(names["my_server"]["ok_name"])          # "_" would break server__tool names


if __name__ == "__main__":
    unittest.main()
