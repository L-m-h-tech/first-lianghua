"""云服务器 IP 文件读取工具。

格式：data/server_ips.txt 每行一个纯 IP，以 # 开头为注释，空行忽略。
读取失败/文件不存在返回空列表（由调用方决定是否报错）。
"""
import os

IPS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "server_ips.txt")


def load_server_ips(path=None):
    """读取 IP 文件 → (ips: list[str], source: str)。
    path: 文件路径，默认项目 data/server_ips.txt。
    返回：空列表 + 原因字符串（文件不存在/为空）或 IP 列表 + 文件路径。"""
    path = path or IPS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            ips = []
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                ips.append(line)
            return ips, path
    except FileNotFoundError:
        return [], f"{path} 文件不存在"
    except Exception as e:
        return [], f"{path} 读取失败: {e}"
