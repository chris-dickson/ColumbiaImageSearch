import sys
import time
import happybase
from datetime import datetime
from ..common.conf_reader import ConfReader

TTransportException = happybase._thriftpy.transport.TTransportException
TException = happybase._thriftpy.thrift.TException
max_errors = 2
# reading a lot of data from HBase at once can be unstable
batch_size = 100
# Is the connection pool causing some issue? Could we use a single connection?


class HBaseIndexerMinimal(ConfReader):

  def __init__(self, global_conf_in, prefix="HBFI_"):
    self.last_refresh = datetime.now()
    self.transport_type = 'buffered'  # this is happybase default
    # self.transport_type = 'framed'
    self.timeout = 4
    # to store count of batches of updates pushed
    self.dict_up = dict()
    self.batch_update_size = 10000
    # could be set in parameters
    self.column_list_sha1s = "info:list_sha1s"
    super(HBaseIndexerMinimal, self).__init__(global_conf_in, prefix)

  def set_pp(self):
    self.pp = "HBaseIndexerMinimal"

  def read_conf(self):
    """ Reads configuration parameters.

    Will read parameters 'host', 'table_sha1infos'...
    from self.global_conf.
    """
    super(HBaseIndexerMinimal, self).read_conf()
    # HBase conf
    self.hbase_host = self.get_required_param('host')
    self.table_sha1infos_name = self.get_required_param('table_sha1infos')
    # TODO: would be deprecated for Kafka ingestion?
    self.table_updateinfos_name = self.get_param('table_updateinfos')
    if self.verbose > 0:
      print_msg = "[{}.read_conf: info] HBase tables name: {} (sha1infos), {} (updateinfos)"
      print print_msg.format(self.pp, self.table_sha1infos_name, self.table_updateinfos_name)
    self.nb_threads = 1
    param_nb_threads = self.get_param('pool_thread')
    if param_nb_threads:
      self.nb_threads = param_nb_threads
    from thriftpy.transport import TTransportException
    try:
      # The timeout as parameter seems to cause issues?...
      #self.pool = happybase.ConnectionPool(size=self.nb_threads, host=self.hbase_host, timeout=10)
      self.pool = happybase.ConnectionPool(size=self.nb_threads, host=self.hbase_host, transport=self.transport_type)
    except TTransportException as inst:
      print_msg = "[{}.read_conf: error] Could not initalize connection to HBase. Are you connected to the VPN?"
      print print_msg.format(self.pp)
      raise inst

      # # Extractions configuration (TO BE IMPLEMENTED)
      # self.extractions_types = self.get_param('extractions_types')
      # self.extractions_columns = self.get_param('extractions_columns')
      # if len(self.extractions_columns) != len(self.extractions_types):
      #     raise ValueError("[HBaseIndexerMinimal.read_conf: error] Dimensions mismatch {} vs. {} for extractions_columns vs. extractions_types".format(len(self.extractions_columns),len(self.extractions_types)))

  def refresh_hbase_conn(self, calling_function, sleep_time=0):
    # this can take up to 4 seconds...
    start_refresh = time.time()
    dt_iso = datetime.utcnow().isoformat()
    print_msg = "[{}.{}: {}] caught timeout error or TTransportException. Trying to refresh connection pool."
    print print_msg.format(self.pp, calling_function, dt_iso)
    sys.stdout.flush()
    time.sleep(sleep_time)
    # This can hang for a long time?
    # Should we add timeout (in ms: http://happybase.readthedocs.io/en/latest/api.html#connection)?
    #self.pool = happybase.ConnectionPool(size=self.nb_threads, host=self.hbase_host, transport=self.transport_type)
    self.pool = happybase.ConnectionPool(timeout=1000, size=self.nb_threads, host=self.hbase_host, transport=self.transport_type)
    print_msg = "[{}.refresh_hbase_conn: log] Refreshed connection pool in {}s."
    print print_msg.format(self.pp, time.time()-start_refresh)

  def check_errors(self, previous_err, function_name, inst=None):
    if previous_err >= max_errors:
      raise Exception("[HBaseIndexerMinimal: error] function {} reached maximum number of error {}. Error was: {}".format(function_name, max_errors, inst))
    return None

  def get_create_table(self, table_name, conn=None, families={'info': dict()}):
    try:
      if conn is None:
        from happybase.connection import Connection
        conn = Connection(self.hbase_host)
      try:
        # what exception would be raised if table does not exist, actually none.
        # need to try to access families to get error
        table = conn.table(table_name)
        # this would fail if table does not exist
        _ = table.families()
        return table
      except Exception as inst:
        print "[get_create_table: info] table {} does not exist (yet)".format(table_name)
        conn.create_table(table_name, families)
        table = conn.table(table_name)
        print "[get_create_table: info] created table {}".format(table_name)
        return table
    except Exception as inst:
      print inst

  def scan_from_row(self, table_name, row_start=None, columns=None, previous_err=0, inst=None):
    self.check_errors(previous_err, "scan_from_row", inst)
    try:
      with self.pool.connection(timeout=self.timeout) as connection:
        hbase_table = connection.table(table_name)
        # scan table from row_start, and accumulate in rows the information of the columns that are needed
        rows = []
        for one_row in hbase_table.scan(row_start=row_start, columns=columns, batch_size=2):
          rows.extend(one_row)
          if self.verbose:
            print("[scan_from_row: log] got {} rows.".format(len(rows)))
            sys.stdout.flush()
        return rows
    except Exception as inst:
      print inst
      # try to force longer sleep time...
      self.refresh_hbase_conn("scan_from_row", sleep_time=4)
      return self.scan_from_row(table_name, row_start, columns, previous_err + 1, inst)

  def get_updates_from_date(self, start_date, previous_err=0, inst=None):
    # start_date should be in format YYYY-MM-DD
    rows = None
    self.check_errors(previous_err, "get_updates_from_date", inst)
    # build row_start as index_update_YYYY-MM-DD
    row_start = "index_update_"+start_date
    print row_start
    columns = [self.column_list_sha1s]
    try:
      rows = self.scan_from_row(self.table_updateinfos_name, row_start=row_start, columns=columns)
    except Exception as inst: # try to catch any exception
      print "[get_updates_from_date: error] {}".format(inst)
      self.refresh_hbase_conn("get_updates_from_date")
      return self.get_updates_from_date(start_date, previous_err+1, inst)
    return rows

  def get_today_string(self):
    return datetime.datetime.today().strftime('%Y-%m-%d')

  def get_next_update_id(self, today=None):
    # get today's date as in format YYYY-MM-DD
    if today is None:
      today = self.get_today_string()
    if today not in self.dict_up:
      self.dict_up = dict()
      self.dict_up[today] = 0
    else:
      self.dict_up[today] += 1
    update_id = "index_update_" + today + "_" + str(self.dict_up[today])
    return update_id, today

  def get_batch_update(self, list_sha1s):
    l = len(list_sha1s)
    for ndx in range(0, l, self.batch_update_size):
      yield list_sha1s[ndx:min(ndx + self.batch_update_size, l)]

  def push_list_updates(self, list_sha1s, previous_err=0, inst=None):
    self.check_errors(previous_err, "push_list_updates", inst)
    today = None
    nb_batch_pushed = 0
    # build batches of self.batch_update_size of images updates
    try:
      # Initialize happybase batch
      with self.pool.connection(timeout=self.timeout) as connection:
        table_updateinfos = self.get_create_table(self.table_updateinfos_name, conn=connection)
        b = table_updateinfos.batch(batch_size=10)
        for batch_list_sha1s in self.get_batch_update(list_sha1s):
          update_id, today = self.get_next_update_id(today)
          b.put(update_id, {self.column_list_sha1s: ','.join(batch_list_sha1s)})
          nb_batch_pushed += 1
        b.send()
    except Exception as inst: # try to catch any exception
      print "[push_list_updates: error] {}".format(inst)
      self.dict_up[self.get_today_string()] = 0
      self.refresh_hbase_conn("push_list_updates")
      return self.push_list_updates(list_sha1s, previous_err+1, inst)
    return nb_batch_pushed

  def get_rows_by_batch(self, list_queries, table_name, columns=None, previous_err=0, inst=None):
    self.check_errors(previous_err, "get_rows_by_batch", inst)
    try:
      with self.pool.connection(timeout=self.timeout) as connection:
        #hbase_table = connection.table(table_name)
        hbase_table = self.get_create_table(table_name)
        # slice list_queries in batches of batch_size to query
        rows = []
        nb_batch = 0
        for batch_start in range(0,len(list_queries), batch_size):
          batch_list_queries = list_queries[batch_start:min(batch_start+batch_size,len(list_queries))]
          rows.extend(hbase_table.rows(batch_list_queries, columns=columns))
          nb_batch += 1
        if self.verbose:
          print("[get_rows_by_batch: log] got {} rows using {} batches.".format(len(rows), nb_batch))
        return rows
    except Exception as inst:
      # try to force longer sleep time...
      self.refresh_hbase_conn("get_rows_by_batch", sleep_time=4)
      return self.get_rows_by_batch(list_queries, table_name, columns, previous_err+1, inst)

  def get_columns_from_sha1_rows(self, list_sha1s, columns, previous_err=0, inst=None):
    rows = None
    self.check_errors(previous_err, "get_columns_from_sha1_rows", inst)
    if list_sha1s:
      try:
        rows = self.get_rows_by_batch(list_sha1s, self.table_sha1infos_name, columns=columns)
      except Exception as inst: # try to catch any exception
        print "[get_columns_from_sha1_rows: error] {}".format(inst)
        self.refresh_hbase_conn("get_columns_from_sha1_rows")
        return self.get_columns_from_sha1_rows(list_sha1s, columns, previous_err+1, inst)
    return rows


    # # Something like this could be used to get precomputed face features
    # # But we should get a JSON listing all faces found features and parse it...
    # # (TO BE IMPLEMENTED)
    # def get_precomp_from_sha1(self, list_sha1s, list_type):
    #     """ Retrieves the 'list_type' extractions results from HBase for the image in 'list_sha1s'.

    #     :param list list_sha1s: list of sha1s of the images for which the extractions are requested.
    #     :param list list_type: list of the extractions requested. They have to be a subset of *self.extractions_types*
    #     :returns (list, list) (res, ok_ids): *res* contains the extractions, *ok_ids* the ids of the 'list_sha1s' for which we retrieved something.
    #     """
    #     res = []
    #     ok_ids = []
    #     print("[get_precomp_from_sha1] list_sha1s: {}.".format(list_sha1s))
    #     rows = self.get_full_sha1_rows(list_sha1s)
    #     # check if we have retrieved rows and extractions for each sha1
    #     retrieved_sha1s = [row[0] for row in rows]
    #     print("[get_precomp_from_sha1] retrieved_sha1s: {}.".format(list_sha1s))
    #     # building a list of ok_ids and res for each extraction type
    #     ok_ids = [[] for i in range(len(list_type))]
    #     res = [[] for i in range(len(list_type))]
    #     list_columns = self.get_columns_name(list_type)
    #     print("[get_precomp_from_sha1] list_columns: {}.".format(list_columns))
    #     for i,sha1 in enumerate(retrieved_sha1s):
    #         for e in range(len(list_type)):
    #             if list_columns[e] in rows[i][1]:
    #                 print("[get_precomp_from_sha1] {} {} {} {}.".format(i,sha1,e,list_columns[e]))
    #                 ok_ids[e].append(list_sha1s.index(sha1))
    #                 res[e].append(np.frombuffer(base64.b64decode(rows[i][1][list_columns[e]]), np.float32))
    #                 #res[e].append(rows[i][1][list_columns[e]])
    #     return res, ok_ids


    # def get_columns_name(self, list_type):
    #     list_columns = []
    #     if self.extractions_types:
    #         for e, extr in enumerate(list_type):
    #             if extr not in self.extractions_types:
    #                 raise ValueError("[HBaseIndexerMinimal.get_columns_name: error] Unknown extraction type \"{}\".".format(extr))
    #             pos = self.extractions_types.index(extr)
    #             list_columns.append(self.extractions_columns[pos])
    #     else:
    #         raise ValueError("[HBaseIndexerMinimal.get_columns_name: error] extractions_types were not loaded")
    #     return list_columns
