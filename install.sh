#!/bin/bash
apt update

apt install -y wget unzip

cd ~

mkdir -p Discord_bot

cd Discord_bot

wget -O main.zip https://github.com/oomplay/Discrord-bot/archive/refs/heads/main.zip

unzip main.zip

rm -f main.zip

cd Discrord-bot-main || { echo "โฟลเดอร์ไม่พบ"; exit 1; }

echo "ติดตั้งและดาวน์โหลดเสร็จสิ้นแล้ว!"
