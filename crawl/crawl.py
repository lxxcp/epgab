# -*- coding:utf-8 -*-
import os, django, sys, datetime, platform
from django.utils import timezone
from utils.general import argvs_get, channel_ids_to_dict, in_exclude_channel
from utils.aboutdb import log
from .spiders import epg_func
from dateutil import tz

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "epg.settings")
django.setup()
from web.models import Channel, Epg
from utils.general import (
    crawl_info,
    xmlinfo,
    dirs,
    add_info_title,
    add_info_desc,
    noepg,
)

recrawl, cname, crawl_dt, save_to_db = argvs_get(sys.argv)

tz_sh = tz.gettz("Asia/Shanghai")

# 抓取入口
def main():
    log_start = ""
    max_crawl_days = crawl_info["max_crawl_days"]
    recrawl_days = crawl_info["recrawl_days"]
    epgs_no = 0  # 记录最大的节目单条数
    # 循环获取每天的数据
    for d in range(max_crawl_days):
        ban_channels = []  # 被BAN掉的频道
        dt = (
            datetime.datetime.now().date() + datetime.timedelta(days=d)
            if not cname
            else crawl_dt
        )
        # 单独使用命令获取某一个频道的信息
        if cname:
            channels = Channel.get_spec_channel(Channel, name=cname)
            max_crawl_days = 1
        else:
            if recrawl and d < recrawl_days:
                recrawl1 = 1
            else:
                recrawl1 = 0
            channels = Channel.get_crawl_channels(Channel, dt, recrawl=recrawl1)
        channel_num = 0  # 已经采集的频道数量（成功与不成功都有）
        failed_channels = []  # 获取失败的频道
        success_num = 0  # 获取成功的频道
        channel_queryset_no = 0
        channel_no = channels.count()  # 所有需要采集的数量
        log(
            "%s\t第%s天 共有：%s 个频道节目表需要获取"
            % (dt.strftime("%Y-%m-%d"), d + 1, channel_no)
        )
        # 循环获取每一频道的数据
        while True:
            if channel_queryset_no >= channel_no:  # 遍历完成所有频道后
                if len(ban_channels) == 0:  # ban列表中无数据，则结束此次采集
                    break
                else:
                    channel = ban_channels[0]
            else:
                channel = channels[0]  # 使用了以后，不能再使用此标记了 如channel[0]使用了，channel[0]就变成别的了
                channel_queryset_no += 1
            channel_num += 1
            msg1 = "%s/%s %s-%s" % (channel_num, channel_no, channel.id, channel.name)
            ret = get_epg(channel, dt)
            if "ban" not in ret:
                ret.update({"ban": 0})
            if ret["ban"] == 1:  # 被BAN掉处理方式
                msg2 = "IP已经被BAN，先采集下一频道%s" % ret["msg"]
                msg5 = ""
                if channel not in ban_channels:
                    ban_channels.append(channel)  # 加入 BAN列表
                    msg5 = "加入BAN列表，"
                log("%s,%s,%s,%s" % (log_start, msg1, msg2, msg5))
                continue
            elif ret["ban"] == 0 and channel in ban_channels:  # 已经获取到的就从BAN列表中删除掉
                ban_channels.pop(channel)
            msg2 = "共获取节目数量:%s,%s" % (len(ret["epgs"]), ret["msg"])
            # 如果只是获取某一频道测试，则显示此epgs信息
            if cname:
                msg1 = "测试---%s-%s" % (msg1, channel.source)
                for ep in ret["epgs"]:
                    print("%s\t%s\t%s" % (ep["starttime"], ep["title"], ep["endtime"]))
            # 有数据才保存
            if len(ret["epgs"]) > 0:
                # 重新获取的频道，保存前删除原来旧数据
                msg1 = "%s-%s" % (msg1, ret["source"])  # 因为可能会更换来源，所以此处选择的是，返回的EPG中的来源
                success_num += 1
                msgx = ""
                if recrawl and channel.recrawl and d < recrawl_days:  # 重新获取的频道需要删除旧数据
                    del_ret = Epg.del_channel_epgs(Epg, channel.id, dt, ret["last_program_date"])
                    msgx = "重新获取，删除%s条数据" % del_ret[0]
                    recrawl_today = 1  # 确定今天是否需要重新采集，重新采集的话，不更新
                else:
                    recrawl_today = 0
                # 直接保存为 XML 文件
                save_epg_to_xml(ret["epgs"], channel, dt)
                msg3 = "已保存为 XML 文件"
                msgall = ";".join([msg1, msgx, msg2, msg3]).replace(";;", ";")
                log(msgall)
                # 更新频道的 最新节目日期及最新抓取时间
                if not recrawl_today:
                    channel.last_program_date = ret["last_program_date"]
                else:  # 重新获取，不更新频道最新节目日期
                    channel.last_crawl_dt = timezone.now()
                channel.save()
            else:
                failed_channels.append("%s %s" % (channel.id, channel.name))
            if not cname:  # 单独获取某一频道信息时，不剔除。
                channels = channels.exclude(id=channel.id)  # 将本条记录从记录列表中剔除
        if cname:  # 如果测试则只获取需要测试的那天数据,并不再做下面的生成xml及html等工作
            return
        msgn1 = "，获取失败的为：%s" % (",".join(failed_channels)) if len(failed_channels) > 0 else ""
        msg_failed = ",%s失败" % (channel_no - success_num) if channel_no - success_num > 0 else ""
        msgn = "第 %s 天，共有%s/%s成功%s%s" % (d + 1, success_num, channel_no, msg_failed, msgn1)
        log(msgn)

# 获取节目表的核心程序，包含重试，换源
def get_epg(channel, dt, func_arg=0):
    log_start = "crawl-crawl-get_epg "
    n = 1
    msg = ""
    channel_ids = channel_ids_to_dict(channel.channel_id)
    channel_id = channel_ids[channel.source]
    while n <= crawl_info["retry_crawl_times"]:  # 重试
        ret = epg_func(channel, channel_id, dt, func_arg=func_arg)
        if "ban" in ret and ret["ban"] == 1:  # 如果被BAN掉，直接进入下一条采集，不再重试等待，否则会浪费很多时间
            return ret
        ret.update({"source": channel.source})
        if len(ret["epgs"]) > 0:  # 有数据，则正常进入下一步骤
            break
        else:
            log("%s-%s-%s 第 %s 次重试失败！%s" % (channel.id, channel.name, channel.source, n, ret["msg"]))
            n += 1

    if n > crawl_info["retry_crawl_times"]:
        msg = "经过%s次重试，%s来源未能获取到 %s 频道数据!%s。错误信息:%s" % (
            n - 1,
            channel.source,
            "%s-%s" % (channel.id, channel.name),
            "尝试更换来源" if crawl_info["change_source"] else "",
            ret["msg"],
        )
        log(msg)
        if crawl_info["change_source"]:  # 换源算法
            channel_ids.pop(channel.source)
            sources = [channel.source]  # 已经测试过的来源
            for source in channel_ids:
                channel_id = channel_ids[source]
                ret = epg_func(channel, channel_id, dt, func_arg=func_arg, source=source)
                ret.update({"source": source})
                if ret["success"] == 1:
                    break
                else:  # 换源也没有获取成功
                    log("%s-%s 更换 %s 来源后，获取信息失败,%s" % (channel.id, channel.name, source, ret["msg"]), level=2)
                    sources.append(source)
            if not ret["success"]:  # 所有来源都无法获取到
                log("%s来源，全部失败" % (",".join(sources)), level=2)
            else:  # 某个来源获取到了数据
                log("%s %s 来源获取成功" % (",".join(sources) + " 来源获取失败，", ret["source"]), level=1)

    return ret

# 将 EPG 数据保存为 XML 文件
def save_epg_to_xml(epgs, channel, dt):
    xmlhead = '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE tv SYSTEM "http://api.torrent-tv.ru/xmltv.dtd"><tv generator-info-name="mxd-epg-xml" generator-info-url="https://epg.mxdyeah.top/">'
    xmlbottom = "</tv>"
    tz = " +0800"
    xmldir = "tvmao.xml"

    with open(xmldir, "w", encoding="utf-8") as f:
        f.write(xmlhead)
        c = '<channel id="%s"><display-name lang="zh">%s</display-name></channel>' % (channel.id, channel.tvg_name)
        f.write(c)
        for epg in epgs:
            start = epg["starttime"].astimezone(tz=tz_sh).strftime("%Y%m%d%H%M%S") + tz
            end = epg["endtime"].astimezone(tz=tz_sh).strftime("%Y%m%d%H%M%S") + tz if epg["endtime"] else start
            title = epg["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;")
            desc = epg["desc"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;")
            programinfo = """<programme start="%s" stop="%s" channel="%s"><title lang="zh">%s</title><desc lang="zh">%s</desc></programme>""" % (start, end, channel.id, title, desc)
            f.write(programinfo)
        f.write(xmlbottom)

    log("crawl-save_epg_to_xml 已经生成 XML 文件：%s" % xmldir)

if __name__ == "__main__":
    main()