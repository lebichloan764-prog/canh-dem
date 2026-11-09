# canh-dem

watcher nền: quét danh sách ví, thử từng phát một rồi gửi.

## cài
```
git clone https://github.com/lebichloan764-prog/canh-dem.git && cd canh-dem
python3 -m venv .venv && . .venv/bin/activate
pip install -q eth-account eth-utils
printf 'DSH_SECRET_EVM_PRIVATE_KEY=%s\n' "<KEY>" > .env
chmod +x run.sh
```

## chạy / trạng thái
```
nohup ./run.sh >> autofire2.log 2>&1 &
tail -n 20 autofire2.log
wc -l fired.json 2>/dev/null
```
