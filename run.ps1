$env:PYTHONPATH = "C:\Users\grazi\Downloads\historian_relay_bot\historian_relay_bot"
Remove-Item "C:\Users\grazi\.historian_relay_bot.lock" -Force -ErrorAction SilentlyContinue
Remove-Item "C:\Users\grazi\.historian_relay_bot.lock.win" -Force -ErrorAction SilentlyContinue
python historian_relay_bot\bot.py

.\run.ps1