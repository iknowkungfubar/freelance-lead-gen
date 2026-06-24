#!/bin/bash
set -e

# Initialize DB and run migrations
python -m freelance_lead_gen init

# Then execute the main command
exec "$@"
