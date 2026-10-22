POSTGAME - coaching on your own ranked games
============================================

What you need
- Python 3.8 or newer (free). Windows: https://www.python.org/downloads/
  During setup, tick "Add python.exe to PATH".
- A free Riot development key from https://developer.riotgames.com
  (sign in with your Riot account; the key is on your dashboard).
  The key expires every 24 hours, so you paste a new one each day you use Postgame.

How to start
1. Unzip this folder anywhere, for example your Desktop.
2. Windows: double-click start-windows.bat
   Mac: double-click start-mac.command (if macOS blocks it, right-click > Open)
3. Your browser opens Postgame at http://localhost:8765
4. Paste your Riot key, enter your Riot ID (Name#TAG), choose your region,
   the queue (Solo/Duo, Flex or both) and how many games to load,
   then press "Load my games". The first load of 20 games takes 1-2 minutes,
   100 games about 4-6 minutes. After that only new games are fetched.

Keep the black window open while you use Postgame. Close it to stop.

Good to know
- Everything stays on your computer: your key is saved in riot_key.txt,
  and your games and focus progress are saved in the data folder.
- After you play more ranked games, press Refresh to load them.
- "Load older games" under Recent games adds 20 more, up to 200.
- Never share your Riot key with anyone. A development key is for your own use.
