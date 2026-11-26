"""Запуск команд агентом (1.6, #4279): включается только строкой exec = on, вывод доходит в обеих оболочках,
таймаут убивает дерево процессов. Только Windows (cmd, powershell, taskkill)."""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import agent  # noqa: E402


@unittest.skipUnless(sys.platform == "win32", "команды станции — Windows")
class ExecTest(unittest.TestCase):
    def tearDown(self):
        agent.CONFIG.pop("exec", None)

    def test_off_by_default(self):
        agent.CONFIG.pop("exec", None)
        self.assertFalse(agent.exec_enabled())
        for v in ("on", "true", "1", "YES"):
            agent.CONFIG["exec"] = v
            self.assertTrue(agent.exec_enabled(), v)
        agent.CONFIG["exec"] = "off"
        self.assertFalse(agent.exec_enabled())

    def test_cmd_output_and_code(self):
        r = agent.run_command("echo привет& exit /b 3", shell="cmd", timeout=30)
        self.assertEqual(r["exit_code"], 3)
        self.assertIn("привет", r["stdout"])
        self.assertFalse(r["timed_out"])

    def test_powershell_quotes_and_unicode(self):
        r = agent.run_command('Write-Output "диск ""C"" свободно"; exit 0', shell="powershell", timeout=60)
        self.assertEqual(r["exit_code"], 0, r["stderr"])
        self.assertIn('диск "C" свободно', r["stdout"])

    def test_timeout_kills(self):
        t0 = time.monotonic()
        r = agent.run_command("ping -n 30 127.0.0.1 >nul", shell="cmd", timeout=2)
        self.assertTrue(r["timed_out"])
        self.assertLess(time.monotonic() - t0, 20)


if __name__ == "__main__":
    unittest.main()
