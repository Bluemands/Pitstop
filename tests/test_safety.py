"""The quarantine deny-list: things that must never be moved, whatever a scanner or a user asks."""
import os
import unittest

from pitstop.util import HOME, PF, PF86, PROGRAMDATA, WINDIR, protection_reason


class DenyList(unittest.TestCase):
    def test_refuses_system_and_profile_roots(self):
        for p in ["C:\\", "D:", WINDIR, WINDIR / "System32", WINDIR / "System32" / "drivers", PF, PF86,
                  PROGRAMDATA, HOME, HOME / "Documents", HOME / "Desktop", PROGRAMDATA / "Microsoft" / "Windows",
                  PF / "Common Files"]:
            with self.subTest(p=str(p)):
                self.assertIsNotNone(protection_reason(p), f"{p} must be protected")

    def test_refuses_near_drive_root(self):
        self.assertIsNotNone(protection_reason("C:\\SomeFolder"))

    def test_allows_ordinary_leftovers(self):
        for p in [PF / "SomeVendor" / "OldApp", HOME / "AppData" / "Local" / "SomeOldTool",
                  PROGRAMDATA / "SomeVendor"]:
            with self.subTest(p=str(p)):
                self.assertIsNone(protection_reason(p))

    def test_case_and_slashes_dont_matter(self):
        self.assertIsNotNone(protection_reason(str(WINDIR).upper().replace("\\", "/")))
        self.assertIsNotNone(protection_reason(os.path.join(str(WINDIR).lower(), "system32")))


if __name__ == "__main__":
    unittest.main()
