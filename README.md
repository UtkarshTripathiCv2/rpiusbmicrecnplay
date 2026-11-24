sudo nano /etc/systemd/system/ducatrise.service


[Unit]
Description=DucatRise HUD Autostart
After=graphical.target network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=ducati
Group=ducati
WorkingDirectory=/home/ducati
Environment=DISPLAY=:0
Environment=XAUTHORITY=/home/ducati/.Xauthority
ExecStartPre=/bin/sleep 5
ExecStart=/usr/bin/python3 /home/ducati/ducatrise5.py
Restart=always
RestartSec=3

[Install]
WantedBy=graphical.target


sudo systemctl daemon-reload
sudo systemctl enable ducatrise.service
sudo systemctl start ducatrise.service


sudo systemctl status ducatrise.service


sudo reboot
