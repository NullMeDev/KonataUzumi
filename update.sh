#!/usr/bin/env bash
set -euxo pipefail

# cd into the repo
cd "$(dirname "$0")"

# update code
git pull origin main

# activate venv & install any new deps
source venv/bin/activate
pip install -r requirements.txt

# restart the service
systemctl restart konatauzumi.service   # or rinkokonoe.service