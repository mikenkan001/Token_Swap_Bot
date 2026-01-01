# UMK Token Trading Bot

A Flask-based web application for automated token swapping on Ethereum and Base networks with scheduled execution capabilities.

## 🚀 Features
- **Multi-Network Support**: Trade on Ethereum Mainnet and Base networks
- **Scheduled Trading**: Execute trades at specific future times
- **Real-time Estimation**: Preview token amounts before execution
- **Manual & Automated**: Both instant and scheduled swap options
- **Trade Management**: View, track, and cancel scheduled trades
- **Transaction Tracking**: Complete history with blockchain explorer links

## 🛠️ Tech Stack
- **Backend**: Flask, Web3.py, Threading
- **Frontend**: HTML/CSS/JavaScript (embedded templates)
- **Blockchain**: Ethereum, Base networks, Uniswap Router contracts
- **Security**: Private key signing, gas validation, input sanitization

## 📦 Installation

1. **Clone and setup**
```bash
git clone <repository-url>
cd umk-token-bot
pip install flask web3
