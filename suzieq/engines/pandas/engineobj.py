import pandas as pd
import numpy as np
from suzieq.utils import SchemaForTable, humanize_timestamp, Schema
from suzieq.engines.base_engine import SqEngineObj
from suzieq.sqobjects import get_sqobject
from suzieq.db import get_sqdb_engine
from suzieq.exceptions import DBReadError, UserQueryError
import dateparser
from datetime import datetime
from pandas.core.groupby import DataFrameGroupBy


class SqPandasEngine(SqEngineObj):
    def __init__(self, baseobj):
        self.ctxt = baseobj.ctxt
        self.iobj = baseobj
        self.summary_row_order = []
        self._summarize_on_add_field = []
        self._summarize_on_add_with_query = []
        self._summarize_on_add_list_or_count = []
        self._summarize_on_add_stat = []
        self._summarize_on_perdevice_stat = []
        self._dbeng = get_sqdb_engine(baseobj.ctxt.cfg, baseobj.table, '',
                                      None)

    @property
    def all_schemas(self) -> Schema:
        return self.ctxt.schemas

    @property
    def schema(self) -> SchemaForTable:
        return self.iobj.schema

    @property
    def cfg(self):
        return self.iobj._cfg

    @property
    def table(self):
        return self.iobj._table

    def _get_ipvers(self, value: str) -> int:
        """Return the IP version in use"""

        if ':' in value:
            ipvers = 6
        elif '.' in value:
            ipvers = 4
        else:
            ipvers = ''

        return ipvers

    def _handle_user_query_str(self, df: pd.DataFrame,
                               query_str: str) -> pd.DataFrame:
        """Handle user query, trapping errors and returning exception

        Args:
            df (pd.DataFrame): The dataframe to run the query on
            query_str (str): pandas query string

        Raises:
            UserQueryError: Exception if pandas query aborts with errmsg

        Returns:
            pd.DataFrame: dataframe post query
        """
        if query_str:
            if query_str.startswith('"') and query_str.endswith('"'):
                query_str = query_str[1:-1]

            try:
                df = df.query(query_str).reset_index(drop=True)
            except Exception as ex:
                raise UserQueryError(ex)

        return df

    def get_valid_df(self, table: str, **kwargs) -> pd.DataFrame:
        """The heart of the engine: retrieving the data from the backing store

        Args:
            table (str): Name of the table to retrieve the data for

        Returns:
            pd.DataFrame: The data as a pandas dataframe
        """
        if not self.ctxt.engine:
            print("Specify an analysis engine using set engine command")
            return pd.DataFrame(columns=["namespace", "hostname"])

        # Thanks to things like OSPF, we cannot use self.schema here
        sch = SchemaForTable(table, self.all_schemas)
        phy_table = sch.get_phy_table_for_table()

        columns = kwargs.pop('columns', ['default'])
        addnl_fields = kwargs.pop('addnl_fields', [])
        view = kwargs.pop('view', self.iobj.view)
        active_only = kwargs.pop('active_only', True)
        hostname = kwargs.get('hostname', [])

        fields = sch.get_display_fields(columns)
        key_fields = sch.key_fields()
        drop_cols = []

        if columns == ['*']:
            drop_cols.append('sqvers')

        aug_fields = sch.get_augmented_fields()

        if 'timestamp' not in fields:
            fields.append('timestamp')

        if 'active' not in fields+addnl_fields:
            addnl_fields.append('active')
            drop_cols.append('active')

        # Order matters. Don't put this before the missing key fields insert
        for f in aug_fields:
            dep_fields = sch.get_parent_fields(f)
            addnl_fields += dep_fields

        for fld in key_fields:
            if fld not in fields+addnl_fields:
                addnl_fields.insert(0, fld)
                drop_cols.append(fld)

        for f in addnl_fields:
            if f not in fields:
                # timestamp is always the last field
                fields.insert(-1, f)

        if self.iobj.start_time:
            try:
                start_time = int(dateparser.parse(
                    self.iobj.start_time.replace('last night', 'yesterday'))
                    .timestamp()*1000)
            except Exception as e:
                print(f"ERROR: invalid time {self.iobj.start_time}: {e}")
                return pd.DataFrame()
        else:
            start_time = ''

        if self.iobj.start_time and not start_time:
            # Something went wrong with our parsing
            print(f"ERROR: unable to parse {self.iobj.start_time}")
            return pd.DataFrame()

        if self.iobj.end_time:
            try:
                end_time = int(dateparser.parse(
                    self.iobj.end_time.replace('last night', 'yesterday'))
                    .timestamp()*1000)
            except Exception as e:
                print(f"ERROR: invalid time {self.iobj.end_time}: {e}")
                return pd.DataFrame()
        else:
            end_time = ''

        if self.iobj.end_time and not end_time:
            # Something went wrong with our parsing
            print(f"ERROR: Unable to parse {self.iobj.end_time}")
            return pd.DataFrame()

        table_df = self._dbeng.read(
            phy_table,
            'pandas',
            start_time=start_time,
            end_time=end_time,
            columns=fields,
            view=view,
            key_fields=key_fields,
            **kwargs
        )

        if not table_df.empty:
            # hostname may not have been filtered if using regex
            if hostname:
                hdf_list = []
                for hn in hostname:
                    df1 = table_df.query(f"hostname.str.match('{hn}')")
                    if not df1.empty:
                        hdf_list.append(df1)

                if hdf_list:
                    table_df = pd.concat(hdf_list)
                else:
                    return pd.DataFrame(columns=table_df.columns.tolist())

            if view == "all" or not active_only:
                table_df.drop(columns=drop_cols, inplace=True)
            else:
                table_df = table_df.query('active') \
                                   .drop(columns=drop_cols)
            if 'timestamp' in table_df.columns and not table_df.empty:
                table_df['timestamp'] = humanize_timestamp(
                    table_df.timestamp, self.cfg.get('analyzer', {})
                    .get('timezone', None))

        return table_df

    def get(self, **kwargs) -> pd.DataFrame:
        """The default get method for all tables

        Use this for a table if nothing special is desired. No table uses
        this routine today.

        Raises:
            NotImplementedError: If no table has been defined

        Returns:
            pd.DataFrame: pandas dataframe of the object
        """
        if not self.iobj.table:
            raise NotImplementedError

        user_query = kwargs.pop('query_str', '')

        df = self.get_valid_df(self.iobj.table, **kwargs)

        df = self._handle_user_query_str(df, user_query)

        return df

    def get_table_info(self, table: str, **kwargs) -> dict:
        """Returns information about the data available for a table

        Used by table show command exclusively.
        Args:
            table (str): Name of the table about which info is desired

        Returns:
            dict: The desired data as a dictionary
        """
        # You can't use view from user because we need to see all the data
        # to compute data required.
        kwargs.pop('view', None)

        all_time_df = self.get_valid_df(table, view='all', **kwargs)
        times = all_time_df['timestamp'].unique()
        ret = {'firstTime': all_time_df.timestamp.min(),
               'latestTime': all_time_df.timestamp.max(),
               'intervals': len(times),
               'allRows': len(all_time_df),
               'namespaces': self._unique_or_zero(all_time_df, 'namespace'),
               'deviceCnt': self._unique_or_zero(all_time_df, 'hostname')}

        return ret

    def _get_table_sqobj(self, table: str, start_time: str = None,
                         end_time: str = None, view=None):
        """Normalize pulling data from other tables into this one function

        Typically pulling data involves calling get_sqobject with a bunch of
        parameters that need to be passed to it, that a caller can forget to 
        pass. A classic example is passing the view, start-time and end-time 
        which is often forgotten. This function fixes this issue.

        Args:
            table (str): The table to retrieve the info from
            verb (str): The verb to use in the get_sqobject call
        """

        return get_sqobject(table)(
            context=self.ctxt,
            start_time=start_time or self.iobj.start_time,
            end_time=end_time or self.iobj.end_time,
            view=view or self.iobj.view)

    def _unique_or_zero(self, df: pd.DataFrame, col: str) -> int:
        """Returns the unique count of a column in a dataframe or 0

        Args:
            df (pd.DataFrame): The dataframe to use
            col (str): The column name to use

        Returns:
            int: Count of unique values
        """
        if col in df.columns:
            return df[col].nunique()
        else:
            return 0

    def summarize(self, **kwargs):
        """Summarize the info about this resource/service.

        There is a pattern of how to do these
        use self._init_summarize():
           - creates self.summary_df, which is the initial pandas dataframe
             based on the table
           - creates self.nsgrp of data grouped by namespace
           - self.ns is the dict to add data to which will be turned into a
             dataframe and then returned

        if you want to simply take a field and run a pandas functon, then use
          self._add_field_to_summary

        at the end of te summarize
        return pd.DataFrame(self.ns).convert_dtypes()

        If you don't override this, then you get a default summary of all columns
        """
        self._init_summarize(self.iobj._table, **kwargs)
        if self.summary_df.empty:
            return self.summary_df

        self._gen_summarize_data()
        self._post_summarize()
        return self.ns_df.convert_dtypes()

    def unique(self, **kwargs) -> pd.DataFrame:
        """Return the unique elements as per user specification

        Raises:
            ValueError: If len(columns) != 1

        Returns:
            pd.DataFrame: Pandas dataframe of unique values for given column
        """
        count = kwargs.pop("count", 0)
        query_str = kwargs.get('query_str', '')

        columns = kwargs.pop("columns", None)

        if query_str:
            getcols = ['*']
        else:
            getcols = columns

        column = columns[0]

        df = self.get(columns=getcols, **kwargs)
        if df.empty:
            return df

        # check if column we're looking at is a list, and if so explode it
        if df.apply(lambda x: isinstance(x[column], np.ndarray), axis=1).all():
            df = df.explode(column).dropna(how='any')

        if not count:
            return (pd.DataFrame({f'{column}': df[column].unique()}))
        else:
            r = df[column].value_counts()
            return (pd.DataFrame({column: r})
                    .reset_index()
                    .rename(columns={column: 'numRows',
                                     'index': column})
                    .sort_values(column))

    def analyze(self, **kwargs):
        raise NotImplementedError

    def aver(self, **kwargs):
        raise NotImplementedError

    def top(self, **kwargs):
        """Default implementation of top.
        The basic fields this assumes are present include the "what" keyword
        which contains the name of the field we're getting the transitions on,
        the "n" field which tells the count of the top entries you're
        looking for, and the reverse field which tells whether you're looking
        for the largest (default, and so reverse is False) or the smallest(
        reverse is True). This invokes the default object's get routine. It
        is upto the caller to ensure that the desired column is in the output.
        """
        what = kwargs.pop("what", None)
        reverse = kwargs.pop("reverse", False)
        sqTopCount = kwargs.pop("count", 5)

        if not what:
            return pd.DataFrame()

        df = self.get(addnl_fields=self.iobj._addnl_fields, **kwargs)
        if df.empty or ('error' in df.columns):
            return df

        if reverse:
            return df.nsmallest(sqTopCount, columns=what, keep="all") \
                     .head(sqTopCount)
        else:
            return df.nlargest(sqTopCount, columns=what, keep="all") \
                     .head(sqTopCount)

    def _init_summarize(self, table: str, **kwargs) -> None:
        """Initialize the data structures for use with generating summary

        Args:
            table (str): Name of table to generate summary for
        """
        kwargs.pop('columns', None)
        columns = ['*']

        df = self.get(columns=columns, **kwargs)
        if 'error' in df.columns:
            self.summary_df = df
            return

        self.summary_df = df
        if df.empty:
            return

        self.ns = {i: {} for i in df['namespace'].unique()}
        self.nsgrp = df.groupby(by=["namespace"], observed=True)

    def _gen_summarize_data(self):
        """Generate the data required for summary"""

        if not self._summarize_on_add_field:
            # Add the only field we truly know to add
            self._summarize_on_add_field = [
                ('deviceCnt', 'hostname', 'nunique'),
            ]
        for field_name, col, function in self._summarize_on_add_field:
            if col != 'namespace' and col != 'timestamp':
                self._add_field_to_summary(col, function, field_name)
                self.summary_row_order.append(field_name)

        for flds in self._summarize_on_add_with_query:
            field_name = flds[0]
            query_str = flds[1]
            field = flds[2]
            if len(flds) == 4:
                func = flds[3]
            else:
                func = 'count'
            fld_df = self.summary_df.query(query_str)
            if not fld_df.empty:
                fld_per_ns = fld_df.groupby(by=['namespace'],
                                            observed=True)[field] \
                    .agg(func)
                for i in self.ns.keys():
                    self.ns[i].update({field_name: fld_per_ns.get(i, 0)})
            else:
                for i in self.ns.keys():
                    self.ns[i].update({field_name: 0})

            self.summary_row_order.append(field_name)

        for field_name, field in self._summarize_on_add_list_or_count:
            self._add_list_or_count_to_summary(field, field_name)
            self.summary_row_order.append(field_name)

        for field_name, query_str, field in self._summarize_on_add_stat:
            if query_str:
                statfld = self.summary_df.query(query_str) \
                                         .groupby(by=['namespace'],
                                                  observed=True)[field]
            else:
                statfld = self.summary_df.groupby(
                    by=['namespace'], observed=True)[field]

            self._add_stats_to_summary(statfld, field_name)
            self.summary_row_order.append(field_name)

        for field_name, query_str, field, func in \
                self._summarize_on_perdevice_stat:
            if query_str:
                statfld = self.summary_df \
                    .query(query_str) \
                    .groupby(by=['namespace', 'hostname'],
                             observed=True)[field].agg(func)
            else:
                statfld = self.summary_df \
                    .groupby(by=['namespace', 'hostname'],
                             observed=True)[field].agg(func)

            self._add_stats_to_summary(statfld, field_name, filter_by_ns=True)
            self.summary_row_order.append(field_name)

    def _post_summarize(self, check_empty_col: str = 'deviceCnt') -> None:
        """Once summary data has been generated, finalize summary dataframe

        Args:
            check_empty_col (str, optional): column name to check to remove
                                             namespace that's empty. 
                                             Defaults to 'deviceCnt'.
        """
        # this is needed in the case that there is a namespace that has no
        # data for this command

        if not check_empty_col:
            check_empty_col = self._check_empty_col

        delete_keys = []
        for ns in self.ns:
            if self.ns[ns][check_empty_col] == 0:
                delete_keys.append(ns)
        for ns in delete_keys:
            del(self.ns[ns])

        ns_df = pd.DataFrame(self.ns)
        if len(self.summary_row_order) > 0:
            ns_df = ns_df.reindex(self.summary_row_order, axis=0)
        self.ns_df = ns_df

    def _add_constant_to_summary(self, field: str, value):
        """And a constant value to specified field name in summary"""
        if field:
            {self.ns[i].update({field: value}) for i in self.ns.keys()}

    def _add_field_to_summary(self, field: str, method: str = 'nunique',
                              field_name: str = None):
        """Add a summary field to summary dataframe based on method on field

        This assumes that self.summary_df has been populated.
        Args:
            field (str): Column name to add in the summary dataframe
            method (str, optional): pandas method name. Defaults to 'nunique'.
            field_name (str, optional): column name to compute on. Defaults to None.
        """
        if not field_name:
            field_name = field
        field_per_ns = getattr(self.nsgrp[field], method)()
        {self.ns[i].update({field_name: field_per_ns.get(i, 0)})
         for i in self.ns.keys()}

    def _add_list_or_count_to_summary(self, field: str,
                                      field_name: str = None) -> None:
        """if there are less than 3 unique things, add as a list, otherwise return the count"""
        if not field_name:
            field_name = field

        count_per_ns = self.nsgrp[field].nunique()

        for n in self.ns.keys():
            if 3 >= count_per_ns[n] > 0:
                # can't do a value_counts on all groups, incase one of the groups other groups doesn't have data
                unique_for_ns = self.nsgrp.get_group(n)[field].value_counts()
                value = unique_for_ns.to_dict()
                # Filter numm entries if category because of how pandas behaves here
                if self.nsgrp[field].dtype[n].name == 'category':
                    value = dict(filter(lambda x: x[1] != 0, value.items()))

            else:
                value = count_per_ns[n]

            self.ns[n].update({field_name: value})

    def _add_stats_to_summary(self, groupedby: DataFrameGroupBy,
                              fieldname: str,
                              filter_by_ns: bool = False) -> None:
        """Takes grouped stats and adds min, max, and median to stats"""

        {self.ns[i].update({fieldname: []}) for i in self.ns.keys()}
        if filter_by_ns:
            {self.ns[i][fieldname].append(groupedby[i].min())
             if i in groupedby else self.ns[i][fieldname].append(0)
             for i in self.ns.keys()}
            {self.ns[i][fieldname].append(groupedby[i].max())
             if i in groupedby else self.ns[i][fieldname].append(0)
             for i in self.ns.keys()}
            {self.ns[i][fieldname].append(
                groupedby[i].median(numeric_only=False))
             if i in groupedby else self.ns[i][fieldname].append(0)
             for i in self.ns.keys()}
        else:
            min_field = groupedby.min()
            max_field = groupedby.max()
            med_field = groupedby.median(numeric_only=False)

            {self.ns[i][fieldname].append(min_field[i])
             if i in min_field else self.ns[i][fieldname].append(0)
             for i in self.ns.keys()}
            {self.ns[i][fieldname].append(max_field[i])
             if i in max_field else self.ns[i][fieldname].append(0)
             for i in self.ns.keys()}
            {self.ns[i][fieldname].append(med_field[i])
             if i in med_field else self.ns[i][fieldname].append(0)
             for i in self.ns.keys()}
