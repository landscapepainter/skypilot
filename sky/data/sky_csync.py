"""CSYNC module"""
import functools
import os
import subprocess
import time
from typing import Any, List, Optional

import click
import psutil

from sky import sky_logging
from sky.utils import common_utils
from sky.utils import db_utils

logger = sky_logging.init_logger(__name__)

_CSYNC_DB_PATH = '~/.sky/sky_csync.db'

_DB = None
_CURSOR = None
_CONN = None
_BOOT_TIME = None


def connect_db(func):

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        global _DB, _CURSOR, _CONN, _BOOT_TIME

        def create_table(cursor, conn):
            cursor.execute("""\
                CREATE TABLE IF NOT EXISTS running_csync (
                csync_pid INTEGER PRIMARY KEY,
                sync_pid INTEGER DEFAULT -1,
                source_path TEXT,
                boot_time FLOAT)""")

            conn.commit()

        if _DB is None:
            db_path = os.path.expanduser(_CSYNC_DB_PATH)
            _DB = db_utils.SQLiteConn(db_path, create_table)

        _CURSOR = _DB.cursor
        _CONN = _DB.conn
        _BOOT_TIME = psutil.boot_time()

        return func(*args, **kwargs)

    return wrapper


@connect_db
def _add_running_csync(csync_pid: int, source_path: str):
    """Given the process id of CSYNC, it should create a row with it"""
    assert _CURSOR is not None
    assert _CONN is not None
    _CURSOR.execute(
        'INSERT INTO running_csync '
        '(csync_pid, source_path, boot_time) '
        'VALUES (?, ?, ?)', (csync_pid, source_path, _BOOT_TIME))
    _CONN.commit()


@connect_db
def _get_all_running_csync_pid() -> List[Any]:
    """Returns all the registerd pid of CSYNC processes"""
    assert _CURSOR is not None
    _CURSOR.execute('SELECT csync_pid FROM running_csync WHERE boot_time=(?)',
                    (_BOOT_TIME,))
    rows = _CURSOR.fetchall()
    csync_pids = [row[0] for row in rows]
    return csync_pids


@connect_db
def _set_running_csync_sync_pid(csync_pid: int, sync_pid: Optional[int]):
    """Given the process id of CSYNC, sets the sync_pid column value"""
    assert _CURSOR is not None
    assert _CONN is not None
    _CURSOR.execute(
        'UPDATE running_csync '
        'SET sync_pid=(?) WHERE csync_pid=(?)', (sync_pid, csync_pid))
    _CONN.commit()


@connect_db
def _get_running_csync_sync_pid(csync_pid: int) -> Optional[int]:
    """Given the process id of CSYNC, returns the sync_pid column value"""
    assert _CURSOR is not None
    _CURSOR.execute(
        'SELECT sync_pid FROM running_csync '
        'WHERE csync_pid=(?) AND boot_time=(?)', (csync_pid, _BOOT_TIME))
    row = _CURSOR.fetchone()
    if row:
        return row[0]
    raise ValueError(f'CSYNC PID {csync_pid} not found.')


@connect_db
def _delete_running_csync(csync_pid: int):
    """Deletes the row with process id of CSYNC from running_csync table"""
    assert _CURSOR is not None
    assert _CONN is not None
    _CURSOR.execute('DELETE FROM running_csync WHERE csync_pid=(?)',
                    (csync_pid,))
    _CONN.commit()


@connect_db
def _get_csync_pid_from_source_path(path: str) -> Optional[int]:
    """Given the path, returns process ID of csync running on it"""
    assert _CURSOR is not None
    _CURSOR.execute(
        'SELECT csync_pid FROM running_csync '
        'WHERE source_path=(?) AND boot_time=(?)', (path, _BOOT_TIME))
    row = _CURSOR.fetchone()
    if row:
        return row[0]
    return None


@click.group()
def main():
    pass


def get_s3_upload_cmd(src_path: str, dst: str, num_threads: int, delete: bool,
                      no_follow_symlinks: bool):
    """Builds sync command for aws s3"""
    config_cmd = ('aws configure set default.s3.max_concurrent_requests '
                  f'{num_threads}')
    subprocess.check_output(config_cmd, shell=True)
    sync_cmd = f'aws s3 sync {src_path} s3://{dst}'
    if delete:
        sync_cmd += ' --delete'
    if no_follow_symlinks:
        sync_cmd += ' --no-follow-symlinks'
    return sync_cmd


def get_gcs_upload_cmd(src_path: str, dst: str, num_threads: int, delete: bool,
                       no_follow_symlinks: bool):
    """Builds sync command for gcp gcs"""
    sync_cmd = (f'gsutil -m -o \'GSUtil:parallel_thread_count={num_threads}\' '
                'rsync -r')
    if delete:
        sync_cmd += ' -d'
    if no_follow_symlinks:
        sync_cmd += ' -e'
    sync_cmd += f' {src_path} gs://{dst}'
    return sync_cmd


def run_sync(src: str, storetype: str, dst: str, num_threads: int,
             interval_seconds: int, delete: bool, no_follow_symlinks: bool,
             csync_pid: int):
    """Runs the sync command to from src to storetype bucket"""
    #TODO(Doyoung): add enum type class to handle storetypes
    storetype = storetype.lower()
    if storetype == 's3':
        sync_cmd = get_s3_upload_cmd(src, dst, num_threads, delete,
                                     no_follow_symlinks)
    elif storetype == 'gcs':
        sync_cmd = get_gcs_upload_cmd(src, dst, num_threads, delete,
                                      no_follow_symlinks)
    else:
        raise ValueError(f'Unsupported store type: {storetype}')

    max_retries = 10
    # interval_seconds/2 is heuristically determined
    # as initial backoff
    initial_backoff = int(interval_seconds / 2)
    backoff = common_utils.Backoff(initial_backoff)
    for _ in range(max_retries):
        try:
            with subprocess.Popen(sync_cmd, start_new_session=True,
                                  shell=True) as proc:
                _set_running_csync_sync_pid(csync_pid, proc.pid)
                if storetype == 's3':
                    # set number of threads back to its default value
                    config_cmd = \
                        ('aws configure '
                         'set default.s3.max_concurrent_requests 10')
                    subprocess.run(config_cmd, shell=True, check=True)
                proc.wait()
                _set_running_csync_sync_pid(csync_pid, -1)
        except subprocess.CalledProcessError:
            # reset sync pid as the sync process is terminated
            _set_running_csync_sync_pid(csync_pid, -1)
            src_to_bucket = (f'\'{src}\' to \'{dst}\' '
                             f'at \'{storetype}\'')
            wait_time = backoff.current_backoff()
            logger.warning('Encountered an error while syncing '
                           f'{src_to_bucket}. Retrying sync '
                           f'in {wait_time}s. {max_retries} more reattempts'
                           ' remaining. Check the log file in ~/.sky/ '
                           'for more details.')
            time.sleep(wait_time)
        else:
            # successfully completed sync process
            break
    else:
        raise RuntimeError(f'Failed to sync {src_to_bucket} after '
                           f'{max_retries} number of retries. Check '
                           'the log file in ~/.sky/ for more'
                           'details') from None


@main.command()
@click.argument('source', required=True, type=str)
@click.argument('storetype', required=True, type=str)
@click.argument('destination', required=True, type=str)
@click.option('--num-threads', required=False, default=10, type=int, help='')
@click.option('--interval-seconds',
              required=False,
              default=600,
              type=int,
              help='')
@click.option('--delete',
              required=False,
              default=False,
              type=bool,
              is_flag=True,
              help='')
@click.option('--no-follow-symlinks',
              required=False,
              default=False,
              type=bool,
              is_flag=True,
              help='')
def csync(source: str, storetype: str, destination: str, num_threads: int,
          interval_seconds: int, delete: bool, no_follow_symlinks: bool):
    """Runs daemon to sync the source to the bucket every INTERVAL seconds.

    Creates an entry of pid of the sync process in local database while sync
    command is runninng and removes it when completed.

    Args:
        source (str): The local path to the directory that you want to sync.
        storetype (str): The type of cloud storage to sync to.
        destination (str): The bucket or subdirectory in the bucket where the
            files should be synced.
        num_threads (int): The number of threads to use for the sync operation.
        interval_seconds (int): The time interval, in seconds, at which to run
            the sync operation.
        delete (bool): Whether or not to delete files in the destination that
            are not present in the source.
        no_follow_symlinks (bool): Whether or not to follow symbolic links in
            the source directory.
    """
    full_src = os.path.abspath(os.path.expanduser(source))
    # If the given source is already mounted with CSYNC, terminate it.
    if _get_csync_pid_from_source_path(full_src):
        _terminate([full_src])
    csync_pid = os.getpid()
    _add_running_csync(csync_pid, full_src)
    while True:
        start_time = time.time()
        run_sync(full_src, storetype, destination, num_threads,
                 interval_seconds, delete, no_follow_symlinks, csync_pid)
        end_time = time.time()
        # Given the interval_seconds and the time elapsed during the sync
        # operation, we compute remaining time to wait before the next
        # sync operation.
        elapsed_time = int(end_time - start_time)
        remaining_interval = max(0, interval_seconds - elapsed_time)
        # sync_pid column is set to 0 when sync is not running
        time.sleep(remaining_interval)


@main.command()
@click.argument('paths', nargs=-1, required=False, type=str)
@click.option('--all',
              '-a',
              default=False,
              is_flag=True,
              required=False,
              help='Terminates all CSYNC processes.')
def terminate(paths: List[str], all: bool = False) -> None:  # pylint: disable=redefined-builtin
    """Terminates all the CSYNC daemon running after checking if all the
    sync process has completed.

    Args:
        paths (List[str]): list of CSYNC-mounted paths
        all (bool): determine either or not to unmount every CSYNC-mounted
            paths

    Raises:
        click.UsageError: when the paths are not specified
    """
    if not paths and not all:
        raise click.UsageError('Please provide the CSYNC-mounted path to '
                               'terminate the CSYNC process.')
    _terminate(paths, all)


def _terminate(paths: List[str], all: bool = False) -> None:  # pylint: disable=redefined-builtin
    """Terminates all the CSYNC daemon running after checking if all the
    sync process has completed.
    """
    # TODO(Doyoung): Currently, this terminates all the CSYNC daemon by
    # default. Make an option of --all to terminate all and make the default
    # behavior to take a source name to terminate only one daemon.
    # Call the function to terminate the csync processes here
    if all:
        csync_pid_set = set(_get_all_running_csync_pid())
    else:
        csync_pid_set = set()
        for path in paths:
            full_path = os.path.abspath(os.path.expanduser(path))
            csync_pid_set.add(_get_csync_pid_from_source_path(full_path))
    while True:
        if not csync_pid_set:
            break
        sync_running_csync_set = set()
        for csync_pid in csync_pid_set:
            # sync_pid is set to -1 when sync is not running
            if _get_running_csync_sync_pid(csync_pid) != -1:
                sync_running_csync_set.add(csync_pid)
        remove_process_set = csync_pid_set.difference(sync_running_csync_set)
        for csync_pid in remove_process_set:
            try:
                psutil.Process(int(csync_pid)).terminate()
            except psutil.NoSuchProcess as e:
                if 'process no longer exists' in str(e):
                    _delete_running_csync(csync_pid)
                    continue
            _delete_running_csync(csync_pid)
            print(f'deleted {csync_pid}')
            csync_pid_set.remove(csync_pid)
        time.sleep(5)


if __name__ == '__main__':
    main()
