#!/bin/bash
apt update

apt install wget -y

apt install unzip -y

cd

mkdir -p Discord_bot

cd Discord_bot

wget https://github.com/oomplay/Discrord-bot/archive/refs/heads/main.zip

unzip main.zip

rm -r main.zip

cd Discrord-bot-main
