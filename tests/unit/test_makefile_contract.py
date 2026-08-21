import pathlib
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class MakefileContractTests(unittest.TestCase):
    def test_terraform_target_matches_backend_free_ci_gate(self):
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        target = makefile.split("\nterraform:\n", 1)[1].split("\n\n", 1)[0]

        commands = [line.strip() for line in target.splitlines() if line.strip()]
        self.assertEqual(
            commands,
            [
                "terraform fmt -check -recursive infra/terraform",
                ("terraform -chdir=infra/terraform init -backend=false -input=false"),
                "terraform -chdir=infra/terraform validate -no-color",
                "terraform -chdir=infra/terraform test -no-color",
            ],
        )


if __name__ == "__main__":
    unittest.main()
