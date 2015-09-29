#    denyhosts sync server
#    Copyright (C) 2015 Jan-Pascal van Best <janpascal@vanbest.org>

#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.

#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.

#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import datetime

from twistar.registry import Registry
from twisted.internet.defer import inlineCallbacks, returnValue

import config
import stats

def _remove_tables(txn):
    print("Removing all data from database and removing tables")
    txn.execute("DROP TABLE IF EXISTS info")
    txn.execute("DROP TABLE IF EXISTS crackers")
    txn.execute("DROP TABLE IF EXISTS reports")
    txn.execute("DROP TABLE IF EXISTS legacy")
    txn.execute("DROP TABLE IF EXISTS history")

def _evolve_database_initial(txn, dbtype):
    if dbtype=="sqlite3":
        autoincrement="AUTOINCREMENT"
    elif dbtype=="MySQLdb":
        autoincrement="AUTO_INCREMENT"

    txn.execute("""CREATE TABLE crackers (
        id INTEGER PRIMARY KEY {},
        ip_address CHAR(15), 
        first_time INTEGER, 
        latest_time INTEGER, 
        total_reports INTEGER, 
        current_reports INTEGER
    )""".format(autoincrement))
    txn.execute("CREATE UNIQUE INDEX cracker_ip_address ON crackers (ip_address)")

    txn.execute("""CREATE TABLE reports(
        id INTEGER PRIMARY KEY {}, 
        cracker_id INTEGER, 
        ip_address CHAR(15), 
        first_report_time INTEGER, 
        latest_report_time INTEGER
    )""".format(autoincrement))
    txn.execute("CREATE INDEX report_first_time ON reports (first_report_time)")
    txn.execute("CREATE UNIQUE INDEX report_cracker_ip ON reports (cracker_id, ip_address)")
    txn.execute("CREATE INDEX report_cracker_first ON reports (cracker_id, first_report_time)")

    txn.execute("""CREATE TABLE legacy(
        id INTEGER PRIMARY KEY {}, 
        ip_address CHAR(15), 
        retrieved_time INTEGER
    )""".format(autoincrement))
    txn.execute("CREATE UNIQUE INDEX legacy_ip ON legacy (ip_address)")
    txn.execute("CREATE INDEX legacy_retrieved ON legacy (retrieved_time)")

def _evolve_database_v1(txn, dbtype):
    txn.execute("""CREATE TABLE info (
        `key` CHAR(32) PRIMARY KEY,
        `value` VARCHAR(255)
    )""")
    if dbtype=="sqlite3":
        txn.execute('INSERT INTO info VALUES ("schema_version", ?)', (str(_schema_version),))
    elif dbtype=="MySQLdb":
        txn.execute('INSERT INTO info VALUES ("schema_version", %s)', (str(_schema_version),))
    txn.execute('INSERT INTO info VALUES ("last_legacy_sync", 0)')

def _evolve_database_v2(txn, dbtype):
    txn.execute("ALTER TABLE crackers ADD resiliency INTEGER")
    txn.execute("CREATE INDEX cracker_qual ON crackers (current_reports, resiliency, latest_time, first_time)")
    txn.execute("CREATE INDEX cracker_first ON crackers (first_time)")
    txn.execute("UPDATE crackers SET resiliency=latest_time-first_time")

def _evolve_database_v3(txn, dbtype):
    if dbtype=="sqlite3":
        txn.execute("DROP INDEX cracker_qual")
    elif dbtype=="MySQLdb":
        txn.execute("ALTER TABLE crackers DROP INDEX cracker_qual")
    txn.execute("CREATE INDEX cracker_qual ON crackers (latest_time, current_reports, resiliency, first_time)")

def _evolve_database_v4(txn, dbtype):
    txn.execute("CREATE INDEX report_latest ON reports (latest_report_time)")

def _evolve_database_v5(txn, dbtype):
    if dbtype=="sqlite3":
        txn.execute("DROP INDEX report_cracker_ip")
    elif dbtype=="MySQLdb":
        txn.execute("ALTER TABLE reports DROP INDEX report_cracker_ip")
    txn.execute("CREATE INDEX report_cracker_ip ON reports (cracker_id, ip_address, latest_report_time)")

def _evolve_database_v6(txn, dbtype):
    # Remove crackers without reports from database. This may have occured
    # because of a bug in controllers.perform_maintenance()
    txn.execute("""
        DELETE FROM crackers 
        WHERE id NOT IN
            ( SELECT cracker_id FROM reports )
        """)

def _evolve_database_v7(txn, dbtype):
    txn.execute("""CREATE TABLE history (
        `date` DATE PRIMARY KEY,
        num_reports INTEGER,
        num_contributors INTEGER, 
        num_reported_hosts INTEGER 
    )""")

    stats.update_recent_history_txn(txn)




_evolutions = {
    1: _evolve_database_v1,
    2: _evolve_database_v2,
    3: _evolve_database_v3,
    4: _evolve_database_v4,
    5: _evolve_database_v5,
    6: _evolve_database_v6,
    7: _evolve_database_v7
}

_schema_version = len(_evolutions)

def _evolve_database(txn):
    print("Evolving database")
    dbtype = config.dbtype

    try:
        txn.execute('SELECT `value` FROM info WHERE `key`="schema_version"')
        result = txn.fetchone()
        if result is not None:
            current_version = int(result[0])
        else:
            print("No schema version in database")
            _evolve_database_initial(txn, dbtype)
            current_version = 0
    except:
        print("No schema version in database")
        _evolve_database_initial(txn, dbtype)
        current_version = 0

    if current_version > _schema_version:
        print("Illegal database schema {}".format(current_version))
        return

    print("Current database schema is version {}".format(current_version))

    while current_version < _schema_version:
        current_version += 1
        print("Evolving database to version {}...".format(current_version))
        _evolutions[current_version](txn, dbtype)

        if dbtype=="sqlite3":
            txn.execute('UPDATE info SET `value`=? WHERE `key`="schema_version"', (str(current_version),))
        elif dbtype=="MySQLdb":
            txn.execute('UPDATE info SET `value`=%s WHERE `key`="schema_version"', (str(current_version),))

    print("Updated database schema, current version is {}".format(_schema_version))

def evolve_database():
    return Registry.DBPOOL.runInteraction(_evolve_database)

@inlineCallbacks
def clean_database():
    yield Registry.DBPOOL.runInteraction(_remove_tables)
    yield Registry.DBPOOL.runInteraction(_evolve_database)

@inlineCallbacks
def check_database_version():
    try:
        rows = yield Registry.DBPOOL.runQuery('SELECT `value` FROM `info` WHERE `key`="schema_version"')
        if rows is not None:
            current_version = int(rows[0][0])
        else:
            print("No schema version in database")
            current_version = 0
    except:
        current_version = 0

    if current_version != _schema_version:
        logging.debug("Wrong database schema {}, expecting {}, exiting".format(current_version, _schema_version))
        print("Wrong database schema {}, expecting {}, exiting".format(current_version, _schema_version))
        from twisted.internet import reactor
        reactor.stop()
    else:
        logging.info("Database schema is up to date (version {})".format(current_version))
        returnValue(current_version)

# FIXME Not the proper way. What if there's a question mark somewhere
# else in the query?
def translate_query(query):
    if config.dbtype == "MySQLdb":
        return query.replace('?', '%s')
    elif config.dbtype == "sqlite3":
        return query
    else:
        print("unsupported database {}".format(config.dbtype))
        return query

def run_query(query, *args):
    return Registry.DBPOOL.runQuery(translate_query(query), args)

def run_operation(query, *args):
    return Registry.DBPOOL.runOperation(translate_query(query), args)

def run_truncate_query(table):
    if config.dbtype == "MySQLdb":
        query = "TRUNCATE TABLE `{}`".format(table)
    elif config.dbtype == "sqlite3":
        query = "DELETE FROM `{}`".format(table)
    else:
        print("unsupported database {}".format(config.dbtype))
    return Registry.DBPOOL.runQuery(query)
