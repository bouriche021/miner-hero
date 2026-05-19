# ⛏️ MINER HERO v2.0

Bitcoin Stratum Miner built in Python.
Works on Android via Termux.

## Features
- Real Stratum protocol connection
- Correct Merkle root calculation
- Multi-threading support (4 threads)
- Auto-reconnect on disconnect
- Real-time hashrate stats
- Low resource usage

## Installation

### Android (Termux)
```bash
pkg install python git
git clone https://github.com/bouriche021/miner-hero.git
cd miner-hero
python3 miner_hero.py 
