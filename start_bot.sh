#!/bin/bash
# Simple bot starter script

cd /root/claude_code_setup/telegram-orchestrator

echo "========================================="
echo "Telegram Orchestrator Bot"
echo "========================================="
echo ""
echo "Bot: @stromae_ccbot"
echo "Your Chat ID: 8158798678"
echo ""
echo "Starting bot..."
echo ""

# Run bot
exec /usr/bin/python3 test_startup.py
