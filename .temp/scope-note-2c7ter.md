# Review scope note

The working tree carries UNRELATED work-in-progress from another session:
a build-ssh feature (src/csk/build_ssh.py, changes in installer.py,
cli.py, closure.py, config.py, builds/*, tests/test_build_ssh.py,
tests/test_install_blockers_regression.py, docs/external-build-repositories.md,
.gitignore, Skillfile.json). Do NOT review or demand changes to those
files; they are out of this task's scope and will not enter the docs PR.

Review ONLY the docs scope of TASK-260821-2c7ter: README.en.md deletion,
docs/reference.md, docs/cli.md additions, README.md first screen,
pyproject.toml readme field, CONTRIBUTING.md, CONTRIBUTING.ru.md, link
sweep. When running the test suite, note that failures caused by the
foreign src changes (if any) are not attributable to this task; the
release-contract subset (tests/test_release_contract.py) is the binding
gate for the readme switch.
