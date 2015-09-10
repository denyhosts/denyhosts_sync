#!/usr/bin/env python

# denyhosts sync server
# Copyright (C) 2015 Jan-Pascal van Best <janpascal@vanbest.org>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import config
import datetime
import logging
import os.path
import socket
import time

from twisted.internet import reactor, threads, task
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python import log
from twistar.registry import Registry

from jinja2 import Template, Environment, FileSystemLoader

import GeoIP

import matplotlib
# Prevent errors from matplotlib instantiating a Tk window
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy

import models
import database
import __init__

def format_datetime(value, format='medium'):
    dt = datetime.datetime.fromtimestamp(value)
    if format == 'full':
        format="EEEE, d. MMMM y 'at' HH:mm:ss"
    elif format == 'medium':
        format="%a %d-%m-%Y %H:%M:%S"
    return dt.strftime(format)

def insert_zeroes(rows, max = None):
    result = []
    index = 0
    if max is None:
        max = rows[-1][0] + 1

    for value in xrange(max):
        if index < len(rows) and rows[index][0] == value:
            result.append(rows[index])
            index += 1
        else:
            result.append((value,0))
    return result

def humanize_number(number, pos):
    """Return a humanized string representation of a number."""
    abbrevs = (
        (1E15, 'P'),
        (1E12, 'T'),
        (1E9, 'G'),
        (1E6, 'M'),
        (1E3, 'k'),
        (1, '')
    )
    if number < 1000:
        return str(number)
    for factor, suffix in abbrevs:
        if number >= factor:
            break
    return '%.*f%s' % (0, number / factor, suffix)

# Functions containing blocking io, call from thread!
def fixup_crackers(hosts):
    gi = GeoIP.new(GeoIP.GEOIP_MEMORY_CACHE)
    for host in hosts:
        try:
            host.country = gi.country_name_by_addr(host.ip_address)
        except Exception, e:
            logging.debug("Exception looking up country for {}: {}".format(host.ip_address, e))
            host.country = ''
        try:
            if config.stats_resolve_hostnames:
                hostinfo = socket.gethostbyaddr(host.ip_address)
                host.hostname = hostinfo[0]
            else:
                host.hostname = host.ip_address
        except Exception, e:
            logging.debug("Exception looking up reverse DNS for {}: {}".format(host.ip_address, e))
            host.hostname = "-"

def make_daily_graph(txn):
    # Calculate start of daily period: yesterday on the beginning of the
    # current hour
    now = time.time()
    dt_now = datetime.datetime.fromtimestamp(now)
    start_hour = dt_now.hour
    dt_onthehour = dt_now.replace(minute=0, second=0, microsecond=0)
    dt_start = dt_onthehour - datetime.timedelta(days=1)
    yesterday = int(dt_start.strftime('%s'))

    txn.execute(database.translate_query("""
        SELECT CAST((first_report_time-?)/3600 AS UNSIGNED INTEGER), count(*)
        FROM reports
        WHERE first_report_time > ?
        GROUP BY CAST((first_report_time-?)/3600 AS UNSIGNED INTEGER)
        ORDER BY first_report_time ASC
        """), (yesterday, yesterday, yesterday))
    rows = txn.fetchall()
    if not rows:
        return
    #logging.debug("Daily: {}".format(rows))
    rows = insert_zeroes(rows, 24)
    #logging.debug("Daily: {}".format(rows))

    x = [dt_start + datetime.timedelta(hours=row[0]) for row in rows]
    y = [row[1] for row in rows]

    # calc the trendline
    x_num = mdates.date2num(x)
    
    z = numpy.polyfit(x_num, y, 1)
    p = numpy.poly1d(z)
    
    xx = numpy.linspace(x_num.min(), x_num.max(), 100)
    dd = mdates.num2date(xx)
    
    fig = plt.figure()
    ax = fig.gca()
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=4))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(humanize_number))
    ax.set_title("Reports per hour")
    ax.plot(x,y, linestyle='solid', marker='o', markerfacecolor='blue')
    ax.plot(dd, p(xx), "b--")
    ax.set_ybound(lower=0)
    fig.autofmt_xdate()
    fig.savefig(os.path.join(config.graph_dir, 'hourly.svg'))
    fig.clf()
    plt.close(fig)
    
def make_monthly_graph(txn):
    # Calculate start of monthly period: last month on the beginning of the
    # current day
    today = datetime.date.today()
    dt_start = today - datetime.timedelta(weeks=4)

    txn.execute(database.translate_query("""
        SELECT date, num_reports
        FROM history
        WHERE date >= ?
        ORDER BY date ASC
        """), (dt_start,))
    rows = txn.fetchall()
    if rows is None or len(rows)==0:
        return

    (x,y) = zip(*rows)

    # calc the trendline
    x_num = mdates.date2num(x)
    
    z = numpy.polyfit(x_num, y, 1)
    p = numpy.poly1d(z)
    
    xx = numpy.linspace(x_num.min(), x_num.max(), 100)
    dd = mdates.num2date(xx)
    
    fig = plt.figure()
    ax = fig.gca()
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=4))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(humanize_number))
    ax.set_title("Reports per day")
    ax.plot(x,y, linestyle='solid', marker='o', markerfacecolor='blue')
    ax.plot(dd, p(xx),"b--")
    ax.set_ybound(lower=0)
    fig.autofmt_xdate()
    fig.savefig(os.path.join(config.graph_dir, 'monthly.svg'))
    fig.clf()
    plt.close(fig)

def make_history_graph(txn):
    # Graph since first record
    txn.execute(database.translate_query("""
        SELECT date FROM history 
        ORDER BY date ASC
        LIMIT 1
        """))
    first_time = txn.fetchall()
    if first_time is not None and len(first_time)>0 and first_time[0][0] is not None:
        dt_first = first_time[0][0]
    else:
        dt_first= datetime.date.today()
    num_days = ( datetime.date.today() - dt_first ).days
    #logging.debug("First day in data set: {}".format(dt_first))
    #logging.debug("Number of days in data set: {}".format(num_days))
    if num_days == 0:
        return

    txn.execute(database.translate_query("""
        SELECT date, num_reports
        FROM history
        ORDER BY date ASC
        """))
    rows = txn.fetchall()
    if rows is None or len(rows)==0:
        return

    (x,y) = zip(*rows)

    # calc the trendline
    x_num = mdates.date2num(x)
    
    z = numpy.polyfit(x_num, y, 1)
    p = numpy.poly1d(z)
    
    xx = numpy.linspace(x_num.min(), x_num.max(), 100)
    dd = mdates.num2date(xx)
    
    fig = plt.figure()
    ax = fig.gca()

    locator = mdates.AutoDateLocator(interval_multiples=False)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.AutoDateFormatter(locator))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(humanize_number))
    ax.set_title("Reports per day")
    if (num_days<100):
        ax.plot(x,y, linestyle='solid', marker='o', markerfacecolor='blue')
    else:
        ax.plot(x,y, linestyle='solid', marker='')
    ax.plot(dd, p(xx),"b--")
    ax.set_ybound(lower=0)
    fig.autofmt_xdate()
    fig.savefig(os.path.join(config.graph_dir, 'history.svg'))
    fig.clf()
    plt.close(fig)

def make_contrib_graph(txn):
    # Number of reporters over days
    txn.execute(database.translate_query("""
        SELECT date FROM history 
        ORDER BY date ASC
        LIMIT 1
        """))
    first_time = txn.fetchall()
    if first_time is not None and len(first_time)>0 and first_time[0][0] is not None:
        dt_first = first_time[0][0]
    else:
        dt_first= datetime.date.today()
    num_days = ( datetime.date.today() - dt_first ).days
    if num_days == 0:
        return

    txn.execute(database.translate_query("""
        SELECT date, num_contributors
        FROM history
        ORDER BY date ASC
        """))
    rows = txn.fetchall()
    if rows is None or len(rows)==0:
        return

    (x,y) = zip(*rows)

    fig = plt.figure()
    ax = fig.gca()
    locator = mdates.AutoDateLocator(interval_multiples=False)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.AutoDateFormatter(locator))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(humanize_number))
    ax.set_title("Number of contributors")
    if (num_days<100):
        ax.plot(x,y, linestyle='solid', marker='o', markerfacecolor='blue')
    else:
        ax.plot(x,y, linestyle='solid', marker='')
    ax.set_ybound(lower=0)
    fig.autofmt_xdate()
    fig.savefig(os.path.join(config.graph_dir, 'contrib.svg'))
    fig.clf()
    plt.close(fig)

_cache = None
_stats_busy = False

@inlineCallbacks
def update_stats_cache():
    global _stats_busy
    global _cache
    if _stats_busy:
        logging.debug("Already updating statistics cache, exiting")
        returnValue(None)
    _stats_busy = True

    logging.debug("Updating statistics cache...")

    # Fill history table for yesterday, when necessary
    yield update_history(None, False)

    now = time.time()
    stats = {}
    stats["last_updated"] = now
    stats["has_hostnames"] = config.stats_resolve_hostnames
    # Note paths configured in main.py by the Resource objects
    stats["static_base"] = "../static"
    stats["graph_base"] = "../static/graphs"
    stats["server_version"] = __init__.version
    try:
        #rows = yield database.run_query("SELECT num_hosts,num_reports, num_clients, new_hosts FROM stats ORDER BY time DESC LIMIT 1")
        stats["num_hosts"] = yield models.Cracker.count()
        stats["num_reports"] = yield models.Report.count()

        rows = yield database.run_query("SELECT count(DISTINCT ip_address) FROM reports") 
        if len(rows)>0:
            stats["num_clients"] = rows[0][0]
        else:
            stats["num_clients"] = 0

        yesterday = now - 24*3600
        stats["daily_reports"] = yield models.Report.count(where=["first_report_time>?", yesterday])
        stats["daily_new_hosts"] = yield models.Cracker.count(where=["first_time>?", yesterday])

        recent_hosts = yield models.Cracker.find(orderby="latest_time DESC", limit=10)
        yield threads.deferToThread(fixup_crackers, recent_hosts)
        stats["recent_hosts"] = recent_hosts

        most_reported_hosts = yield models.Cracker.find(orderby="total_reports DESC", limit=10)
        yield threads.deferToThread(fixup_crackers, most_reported_hosts)
        stats["most_reported_hosts"] = most_reported_hosts

        logging.info("Stats: {} reports for {} hosts from {} reporters".format(
            stats["num_reports"], stats["num_hosts"], stats["num_clients"]))

        if stats["num_reports"] > 0:
            yield Registry.DBPOOL.runInteraction(make_daily_graph)
            yield Registry.DBPOOL.runInteraction(make_monthly_graph)
            yield Registry.DBPOOL.runInteraction(make_contrib_graph)
            yield Registry.DBPOOL.runInteraction(make_history_graph)

        if _cache is None:
            _cache = {}
        _cache["stats"] = stats
        _cache["time"] = time.time()
        logging.debug("Finished updating statistics cache...")
    except Exception, e:
        log.err(_why="Error updating statistics: {}".format(e))
        logging.warning("Error updating statistics: {}".format(e))

    _stats_busy = False

@inlineCallbacks
def render_stats():
    global _cache
    logging.info("Rendering statistics page...")
    if _cache is None:
        while _cache is None:
            logging.debug("No statistics cached yet, waiting for cache generation to finish...")
            yield task.deferLater(reactor, 1, lambda _:0, 0)

    now = time.time()
    try:
        env = Environment(loader=FileSystemLoader(config.template_dir))
        env.filters['datetime'] = format_datetime
        template = env.get_template('stats.html')
        html = template.render(_cache["stats"])

        logging.info("Done rendering statistics page...")
        returnValue(html)
    except Exception, e:
        log.err(_why="Error rendering statistics page: {}".format(e))
        logging.warning("Error creating statistics page: {}".format(e))

def update_history_txn(txn, date, overwrite):
    """date should be a datetime.date or None, indicating yesterday. 
    overwrite whould be True when the data should be overwritten when it exists"""
    try:
        if date is None:
            date = datetime.date.today() - datetime.timedelta(days = 1)

        txn.execute(database.translate_query("SELECT 1 FROM history WHERE date=?"), (date,)) 
        rows = txn.fetchall()
        date_exists = rows is not None and len(rows)>0
        logging.debug("Date {} exists in history table: {}".format(date, date_exists))

        if date_exists and not overwrite:
            return

        logging.info("Updating history table for {}".format(date))
        start = time.mktime(date.timetuple())
        end = start + 24*60*60
        #logging.debug("Date start, end: {}, {}".format(start, end))

        txn.execute(database.translate_query("""
                SELECT  COUNT(*), 
                        COUNT(DISTINCT cracker_id),
                        COUNT(DISTINCT ip_address) 
                FROM reports 
                WHERE (first_report_time>=? AND first_report_time<?) OR
                      (latest_report_time>=? AND latest_report_time<?)
                """), (start, end, start, end))
        rows = txn.fetchall()
        if rows is None or len(rows)==0:
            return

        num_reports = rows[0][0]
        num_hosts = rows[0][1]
        num_reporters = rows[0][2]
        logging.debug("Number of reporters: {}".format(num_reporters))
        logging.debug("Number of reports: {}".format(num_reports))
        logging.debug("Number of reported hosts: {}".format(num_hosts))

        if date_exists and overwrite:
            txn.execute(database.translate_query("""
                UPDATE history
                SET num_reports=?, num_contributors=?, num_reported_hosts=?
                WHERE date=?
                """), (num_reports, num_reporters, num_hosts, date))
        else:
            txn.execute(database.translate_query("""
                INSERT INTO history
                    (date, num_reports, num_contributors, num_reported_hosts)
                    VALUES (?,?,?,?)
                """), (date, num_reports, num_reporters, num_hosts))
    except Exception, e:
        log.err(_why="Error updating history: {}".format(e))
        logging.warning("Error updating history: {}".format(e))

def update_history(date, overwrite):
    "date should be a datetime.date or None, indicating yesterday"
    return Registry.DBPOOL.runInteraction(update_history_txn, date, overwrite)


# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
