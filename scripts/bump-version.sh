#!/bin/sh
# Set the release version everywhere it lives: package, compose.yaml and the TrueNAS catalog app.
# Usage: scripts/bump-version.sh 0.1.7   (then commit, push, tag v0.1.7; release.yml checks they all match)
set -eu
v="$1"
echo "$v" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "usage: $0 X.Y.Z" >&2; exit 2; }
cd "$(dirname "$0")/.."
sed -i.bak -E "s/^version = \"[^\"]+\"/version = \"$v\"/" pyproject.toml
sed -i.bak -E "s/^__version__ = \"[^\"]+\"/__version__ = \"$v\"/" loxbridge/__init__.py
sed -i.bak -E "s#(image: ghcr.io/andileeb/loxbridge:)[0-9.]+#\1$v#" compose.yaml
sed -i.bak -E "s/^(app_version|version): .*/\1: $v/" truenas/loxbridge/app.yaml
sed -i.bak -E "s/^(    tag: )\"[0-9.]+\"/\1\"$v\"/" truenas/loxbridge/ix_values.yaml
rm -f pyproject.toml.bak loxbridge/__init__.py.bak compose.yaml.bak truenas/loxbridge/*.bak
uv lock -q
grep -n "$v" pyproject.toml loxbridge/__init__.py compose.yaml truenas/loxbridge/app.yaml truenas/loxbridge/ix_values.yaml
