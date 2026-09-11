#!/usr/bin/env python3
"""批量部署 sina_proxy_server.py 到 11 台阿里云 ECS 并启动验证。
用法: python tools/deploy_servers.py [--ips 8.1.1.1,8.1.1.2,...] [--start-only]
默认从下方 IPS 列表读取。"""
import sys, os, time, threading
import paramiko

IPS = [
    "8.156.69.136", "8.156.73.52", "8.156.69.2", "8.156.73.27", "8.156.72.196",
    "47.109.195.37", "8.156.69.191", "8.156.78.133", "8.156.66.174", "8.137.94.172",
    "47.108.206.1",
]
USER = "ecs-user"
PWD = "Lmh204929182"
PORT = 9001
GAP = 0.7
LOCAL_SRC = os.path.join(os.path.dirname(__file__), "sina_proxy_server.py")
REMOTE_PATH = "/home/ecs-user/sina_proxy_server.py"
REMOTE_CMD = f"nohup python3 {REMOTE_PATH} --port {PORT} --gap {GAP} > server.log 2>&1 & echo STARTED_PID=$!"

def ssh_conn(ip):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, port=22, username=USER, password=PWD, timeout=15)
    return c

def deploy_one(ip):
    try:
        c = ssh_conn(ip)
        # 1) 上传脚本
        sftp = c.open_sftp()
        sftp.put(LOCAL_SRC, REMOTE_PATH)
        sftp.close()
        # 2) 先杀掉旧进程再启动
        c.exec_command("pkill -f sina_proxy_server.py; sleep 1")
        time.sleep(1)
        _, out, err = c.exec_command(REMOTE_CMD)
        out.read(); err.read()
        time.sleep(1.5)
        # 3) 验证本地进程 + HTTP 服务
        _, out, _ = c.exec_command("pgrep -f sina_proxy_server.py | wc -l")
        pcount = out.read().decode().strip()
        _, out, _ = c.exec_command(f"curl -s -m 5 http://127.0.0.1:{PORT}/health")
        health = out.read().decode().strip()
        c.close()
        ok = '"status": "ok"' in health and int(pcount.strip() or 0) >= 1
        return ip, ok, f"进程数={pcount} health={health[:60]}"
    except Exception as e:
        return ip, False, f"{type(e).__name__}: {e}"

def main():
    args = sys.argv[1:]
    ips = IPS
    if "--ips" in args:
        i = args.index("--ips")
        ips = [x.strip() for x in args[i+1].split(",") if x.strip()]
    results = []
    threads = []
    lock = threading.Lock()
    def _run(ip):
        r = deploy_one(ip)
        with lock:
            results.append(r)
            print(f"[{'OK' if r[1] else 'FAIL'}] {r[0]}: {r[2]}", flush=True)
    for ip in ips:
        t = threading.Thread(target=_run, args=(ip,))
        t.start(); threads.append(t)
    for t in threads:
        t.join()
    ok_n = sum(1 for r in results if r[1])
    print(f"\n完成: {ok_n}/{len(ips)} 台部署成功")
    if ok_n < len(ips):
        for ip, ok, msg in results:
            if not ok:
                print(f"  FAIL {ip}: {msg}")

if __name__ == "__main__":
    main()
