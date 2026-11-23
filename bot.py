import baostock as bs
import requests

SEND_KEY = "在这里粘贴你的SendKey"

def send_wechat(msg):
    url = f"https://sctapi.ftqq.com/{SEND_KEY}.send"
    requests.post(url, data={"title":"股票信号提醒", "desp":msg})

def check_stock():
    lg = bs.login()
    if lg.error_code != '0':
        return "登录失败"
    
    rs = bs.query_history_k_data_plus("000001.sh",
        "date,close",
        start_date="2026-11-01",
        end_date="2026-11-23",
        frequency="d", adjustflag="3")
    
    data_list = []
    while (rs.error_code == '0') & rs.next():
        data_list.append(rs.get_row_data())
    bs.logout()

    if len(data_list) < 2:
        return "数据不足"
    
    latest_close = float(data_list[-1][1])
    pre_close = float(data_list[-2][1])
    change = (latest_close - pre_close)/pre_close * 100
    res = f"上证指数\n昨日收盘：{pre_close}\n今日收盘：{latest_close}\n涨跌幅：{change:.2f}%"
    return res

if __name__ == "__main__":
    result = check_stock()
    send_wechat(result)
