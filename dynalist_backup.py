#!/usr/bin/python3 -B
import argparse
import copy
import glob
import hashlib
import io
import json
import logging
import lxml.etree as ElementTree  # sudo apt install python3-lxml
import os
import re
import requests
import shlex
import subprocess
import sys

# API token location. First file found is used.
API_TOKEN_FILES = [
    os.path.expanduser('~/.config/dynalist-backup-token.ini'),
    os.path.expanduser('~/.config/secret/dynalist-backup-token.ini'),
    '/run/user/%d/dynalist-token' % os.getuid()
]


# Directory with data. First directory which is found is used.
DATA_DIRECTORIES = [
    os.path.expanduser('~/.local/share/dynalist-backup'),
    os.path.expanduser('~/.dynalist-backup'),    
    os.path.expanduser('~/.config/dynalist-backup'),    
    '/tmp/dynalist-export'
]

# Cache directory, used only when --cache is passed.
API_CACHE_PREFIX = '/tmp/dynalist-backup-cache/cache-'

class DynalistApi:
    """
    Encapsulated Dynalist API with caching
    """
    def __init__(self, *, from_cache=False):
        self.from_cache = from_cache
        self.sess = requests.Session()
        self.logger = logging.getLogger('api')

        for api_token_name in API_TOKEN_FILES:
            try:
                with open(api_token_name, 'r', encoding='utf-8') as f:
                    self.api_token = f.read().strip()
                break
            except FileNotFoundError:
                pass
        else:
            raise Exception('Cannot find dynalist token file, was looking at %r' % (API_TOKEN_FILES))
        
        self.api_cache_prefix = API_CACHE_PREFIX
        
        if not self.from_cache:
            # We could imagine "write-only" cache mode, but for now, we do not bother.
            self.api_cache_prefix = None
            
        self.logger.debug('API ready: token from %r, from_cache %r, api_cache_prefix %r' % (
            api_token_name, self.from_cache, self.api_cache_prefix))

        if self.api_cache_prefix:
            os.makedirs(os.path.dirname(self.api_cache_prefix), exist_ok=True)
        
    def call(self, path, args):
        """Invoke dynalist API, return json"""
        name_last = path
        if args:
            params_str = json.dumps(args, sort_keys=True, separators=(',', ':'))
            if len(params_str) > 64:
                params_str = hashlib.sha1(params_str.encode('utf-8')).hexdigest()
            name_last += '--' + params_str
        
        if self.api_cache_prefix:
            log_name = self.api_cache_prefix + name_last.replace('/', '--')

            if self.from_cache and os.path.exists(log_name):
                self.logger.debug('Filled from cache: %r' % (log_name, ))
                with open(log_name, 'r') as f: 
                    return json.load(f)

        self.logger.debug('Making request: %r %s' % (path, repr(args)[:32], ))
        
        r = self.sess.post('https://dynalist.io/api/v1/' + path, 
                           json.dumps(dict(token=self.api_token, **args)))
        r.raise_for_status()    
        rv = r.json()        
        if rv['_code'] != 'Ok' or rv.get('_msg', None):
            raise Exception('API call failed: (%r, %r) -> (%r, %r)' % (
                path, args, rv['_code'], rv.get('_msg', None)))
        if self.api_cache_prefix:
            with open(log_name, 'w') as f: 
                f.write(r.text)
                
        return rv


class FileWriter:
    """
    Writes output files in a "smart" way:
    - Do not override files if contents are the same,
    - Keep list of written files, remove the pre-existing files which 
      were not written this time.

    The behaviour is logically equivalent to doing "rm -r" on output dir, then writing
    output files -- but the ctime/inode does not change if the file contents did not change
    either.
    """    
    
    def __init__(self, datadir, dry_run):
        self.datadir = os.path.normpath(os.path.abspath(datadir))
        self.dry_run = dry_run
        self.logger = logging.getLogger('writer')

        if not dry_run:
            assert os.path.isdir(self.datadir), \
                'Data directory %r not found' % self.datadir

        self.logger.debug('Writer ready, datadir %r, dry_run %r' % (datadir, dry_run))
        # Names generated before. Used to ensure non-overriding of files. Set of absolute paths.
        self._files_made = set()

        self._unique_names = set()

        # change list, used for generating git commit messages.
        # list of (action: str, filename: str) tuples
        self._updates = list()

        # A short-diff message, usable for git commit.
        # set by finalize() function
        self.short_diff_message = None
        
        self._num_same = 0
        self._num_changed = 0
        
    def check_git(self):
        """Make sure the output directory is a git repo
        Raise if not"""
        assert os.path.isdir(os.path.join(self.datadir, '.git')), \
            'Data directory %r not found or does not have a git repo' % self.datadir
        
    def is_possible_output(self, fname):
        """To prevent cleanup from deleting too much, we require each file we write
        to be matched by this function.

        If we run a cleanup, and we find the file which is not matched by this function, we abort
        entire cleanu and ask for user's help.
        """
        return fname.endswith('.json') or fname.endswith('.txt')
        
    def make_unique_name(self, base, *, suffix=''):
        """
        Generate unique filename  or file prefix.
        Append numbers to "base" until (base + suffix) does not match any files made nor
        any previous result of this function.
        """

        unique_str = ''
        unique_count = 0
        while True:
            fname = os.path.join(self.datadir, base + unique_str + suffix)
            assert fname.startswith(self.datadir + '/')
            if fname not in self._files_made and fname not in self._unique_names:
                break            
            assert make_unique, 'writing same file twice: %r' % (fname, )
            unique_count += 1
            unique_str = '-%d' % unique_count

        # We could add to self._files_made, but that'd mess up final stats.
        self._unique_names.add(fname)
        return base + unique_str
                
    def make_data_file(self, fname_rel, *, contents=None, data=None):
        """
        Write @p contents (bytestring) to (@p fname_rel + @p suffix), which
          is a path relative to output directory.
        if @p contents is None, serialize @p data to json and write it.
        
        This function raises if this file was already written this session,

        """
        if contents is None:
            contents = json.dumps(data, sort_keys=True, indent=4) + '\n'
        else:
            assert data is None            
        assert not os.path.isabs(fname_rel), 'must be relative: %r' % (fname_rel, )

        fname = os.path.join(self.datadir, fname_rel)
        assert fname.startswith(self.datadir + '/')
        assert self.is_possible_output(fname), \
            'Wanted to write %r but is_possible_output() returns False' % (fname, )
            
        self._files_made.add(fname)
        action = 'create'
        try:
            with open(fname, 'r', encoding='utf-8') as f:
                if f.read() == contents:
                    self._num_same += 1
                    return
            self._num_changed += 1
            action = 'update'
        except (FileNotFoundError, UnicodeDecodeError):
            pass

        self._updates.append((action, fname))
        
        if self.dry_run:
            self.logger.info('dry-run: would %s %r' % (action, fname, ))
        else:
            self.logger.debug('Writing (%s) %r' % (action, fname, ))
            os.makedirs(os.path.dirname(fname), exist_ok=True)
            with open(fname, 'w', encoding='utf-8') as f:
                f.write(contents)


    def try_read_json(self, fname_rel):
        """Try to read json from given rel_path
        @returns json contents  -- if file is found.
                 None           -- if file is not found
           raises on all other errors
        """
        fname = os.path.join(self.datadir, fname_rel)
        try:
            with open(fname, 'r', encoding='utf-8') as f:
                contents = f.read()
        except FileNotFoundError:
            return None
        rj = json.loads(contents)
        # If we read None, it'll be ambivious vs "file not found". We do not expect this
        # to happen, so raise.
        assert rj is not None, 'try_read_json found None object in %r' % (fname_rel, )
        return rj
            
                    
    def finalize(self, delete_others=False):
        """Print update statistics.
        If @p delete_others is True, the enumerate output location and remove
        all files not written this session

        @returns potential commit message
        """

        assert self.short_diff_message is None, 'Finalize() called twice'
        
        # Get list of files which can be potentially cleaned up.
        to_clean = list()
        suspicious = list()
        empty_dirs = set()
        
        for dirpath, dirnames, filenames in os.walk(self.datadir, onerror=_raise):
            if '.git' in dirnames:
                dirnames.remove('.git')
            empty_dirs.update(os.path.join(dirpath, x) for x in dirnames)

            for fname in[os.path.join(dirpath, x) for x in filenames]:
                if fname in self._files_made:
                    continue
                to_clean.append(fname)
                self._updates.append(('delete', fname))
                if not self.is_possible_output(fname):                    
                    suspicious.append(fname)                    
        to_clean.sort()

        nonempty_dirs = {self.datadir}
        for dirpath in sorted(set(os.path.dirname(x) for x in self._files_made)):
            dn = dirpath
            while len(dn) > len(self.datadir):
                nonempty_dirs.add(dn)
                dn = os.path.dirname(dn)


        # For commit message, only .txt files matter
        # (and we only put basenames without extensions, too)
        last_action = None
        detailed_diff_tags = []
        count_by_type = dict()
        for action, fname in sorted(self._updates):
            if fname.endswith('.txt'):
                count_by_type[action] = count_by_type.get(action, 0) + 1
                msg = repr(os.path.splitext(os.path.basename(fname))[0])
                if action != last_action:
                    msg = action + ' ' + msg
                    last_action = action
                detailed_diff_tags.append(msg)
            else:
                count_by_type['other'] = count_by_type.get('other', 0) + 1
                
        detailed_diff = ', '.join(detailed_diff_tags)
        if detailed_diff == '':
            self.short_diff_message = 'no changes'
        elif len(detailed_diff) < 120 or len(detailed_diff_tags) <= 2:
            self.short_diff_message = detailed_diff
        else:
            # Message too long, just show counts
            self.short_diff_message = ', '.join(
                '%s %d' % (action, count) for action, count in sorted(count_by_type.items()))
            
        # Remove empty dirs, starting from deepest filenames
        empty_dirs.difference_update(nonempty_dirs)
        to_clean += [x + '/' for x in sorted(empty_dirs, key=lambda x: (-x.count('/'), x))]
        
        msg = ('Outputs: %d same (in %d folders), %d changed, %d new, %d to-remove' % (
            self._num_same, len(nonempty_dirs), self._num_changed, len(self._files_made) - self._num_same - self._num_changed,            
            len(to_clean)))
        msg2 = 'Details: ' + detailed_diff
        if self._num_same == len(self._files_made):
            self.logger.debug(msg)
            self.logger.debug(msg2)
        else:
            self.logger.info(msg)
            self.logger.info(msg2)

        if suspicious:
            self.logger.warning('Found suspicious files in output dir (%d), cleanup disabled: %s' % (
                len(suspicisous), ' ' .join(map(shlex.quote, sorted(suspicious)[:10]))))
            
        if not to_clean:
            self.logger.debug('Nothing to clean up')
        elif delete_others:
            if suspicious:
                raise SystemExit('FATAL: Cannot cleanup: %d suspicious files' % (len(suspicious)))
            self.logger.info('Deleting %d old file(s)' % (len(to_clean)))
            if to_clean:
                self.logger.debug('.. some names to clean: %r' % (to_clean[:5]))
            for fname in to_clean:
                if self.dry_run:
                    self.logger.info('dry-run: would remove %r' % (fname, ))
                elif fname.endswith('/'):
                    self.logger.debug('Removing dir: %r' % (fname, ))
                    os.rmdir(fname)
                else:
                    self.logger.debug('Removing file: %r' % (fname, ))
                    os.unlink(fname)

    def git_commit(self, *, title='', dry_run):
        """If there are any changes, commit them"""
        changes = subprocess.check_output('git status --porcelain'.split(), cwd=self.datadir).decode('utf-8').splitlines()
        if not changes:
            self.logger.debug('git up to date, not committing')
            return
        self.logger.debug('git status returned %d lines' % (len(changes)))

        cmd = ['git', 'add', '--all']
        if dry_run:
            cmd += ['--dry-run']
        cmd += ['--', '.']
        self.logger.debug('Running: ' + ' ' .join(map(shlex.quote, cmd)))
        subprocess.check_call(cmd, cwd=self.datadir)

        message = self.short_diff_message
        cmd = ['git', 'commit', '-m', message, '--quiet'] 
        if dry_run:
            cmd += ['--dry-run', '--short']
        self.logger.debug('Running: ' + ' ' .join(map(shlex.quote, cmd)))
        subprocess.check_call(cmd, cwd=self.datadir)

        self.logger.info('Made a git commit: %s' % (message, ))
                    
def _raise(x):
    """Workaround for python's hate of one-liners"""
    raise x
                

class Downloader:
    """
    Download raw data, give each record a name, and save them to temporary directory.
    Assigns the names to each record, too.
    """
    def __init__(self, writer):        
        self._writer = writer               
        self.logger = logging.getLogger('downloader')

        # Output: file index, including folder. List of api(file/list) objects,
        # augemented with 'version' and '_path' fields
        self.file_index = None

        # Output: document contents. Map filename -> api(doc/read) objects.
        self.doc_contents = None
        
    def sync_all(self, api):
        """Takes @p api object, syncs all the raw data to the local directory"""
        raw_file_list = self._sync_file_list(api)
        versions_info = self._get_versions_info(api, raw_file_list)
        self.file_index = self._assign_obj_filenames(raw_file_list)
        
        self.doc_contents = self._get_contents(api, self.file_index, versions_info)

        self._make_processed_files(self.doc_contents)
        
        
    def _sync_file_list(self, api):
        """Update file list, generate name for each file

        @returns raw_file_list -- augmented list of files (like file/list API, but with extra version fields)
        """
        files = api.call('file/list', {})
        # Guard against API changes
        assert files.keys() == {'root_file_id', 'files', '_code'}, 'bad files keys: %r' % (files.keys(), )
        
        # Record raw list. Leading underscore guarantees no collisions.
        self._writer.make_data_file('_raw_list.json', data=files)
        return files


    def _get_versions_info(self, api, files):
        # Augment data with version numbers -- we fetch version for each document right away.
        all_file_ids = sorted([x['id'] for x in files['files'] if x['type'] != 'folder'])
        versions = api.call('doc/check_for_updates', {"file_ids": all_file_ids})
        assert versions.keys() == {'versions', '_code'}, 'bad versions keys: %r' % (versions.keys(), )

        self._writer.make_data_file('_raw_versions.json', data=versions)
        versions_set = set(versions['versions'])
        file_id_set = set(x['id'] for x in files['files'] if x['type'] != 'folder')
        
        self.logger.debug('Versions call: got %d matches, %d files without versions, %d versions without files' % (
            len(versions_set.intersection(file_id_set)),
            len(file_id_set.difference(versions_set)),
            len(versions_set.difference(file_id_set))))
                
        return versions['versions']

    def _assign_obj_filenames(self, raw_list):
        """
        Walk hierarchy, assign a filename to each file.

        @param raw_list -- output of _sync_file_list
        @return 
        """
        # (could have done it recursively, but I don't like too many parameters)                              
        to_process = {f["id"]: f for f in raw_list['files']}
        todo = [('', raw_list['root_file_id'])]

        # We produce a list of files in pre-order (same order as shown in UI)
        file_list = list()
        
        while todo:
            path_prefix, file_id = todo.pop(0)
            file_obj = to_process.pop(file_id)

            # Generate the unique name for this object. Note we strip leading/trailing underscores
            # and dots to prevent surprises, or dirnames overlapping filenames.
            base_name = path_prefix + (re.sub('([^A-Za-z0-9()_. -]+)', '_', file_obj['title']).strip('_. -')
                                       or 'unnamed')            
            if file_id == raw_list['root_file_id']:
                base_name = '_root_file'                
                
            # Write the raw file right away
            fname = self._writer.make_unique_name(base_name)
            obj_type = file_obj['type']
            assert obj_type in ['folder', 'document']
            
            next_prefix = fname + '/'            
            file_obj_new = dict(file_obj)
            # Force first-level folders to be toplevel
            if file_id == raw_list['root_file_id']:
                assert obj_type == 'folder'
                next_prefix = ''
                file_obj_new['_is_root'] = True

            todo = [(next_prefix, cid) for cid in file_obj.get('children', [])] + todo

            file_obj_new['_path'] = fname
            file_list.append(file_obj_new)
                                
        assert not to_process, 'Orphan file entries found: %r' % (sorted(to_process.keys()), )
               
        # To avoid too much churn, only store name/id pairs.
        self._writer.make_data_file('_raw_filenames.json', data=[dict(_path=x['_path'], id=x['id'])
                                                                for x in file_list])

        return file_list


    def _get_contents(self, api, file_index, versions_info):
        doc_contents = dict()
        num_changed = 0        
        
        for file_obj in file_index:
            if file_obj['type'] != 'document':
                continue
            path = file_obj['_path']
            obj_id = file_obj['id']
            expected_version = versions_info.get(obj_id, None)

            last_contents = self._writer.try_read_json(path + '.c.json')

            if (last_contents is not None and expected_version is not None and
                last_contents["version"] == expected_version):
                # self.logger.debug('Using cached document %r (id %r), version %r' % (path, obj_id, expected_version))
                contents = last_contents
            else:
                self.logger.debug('Fatching document %r (id %r), version: expected %r, stored %r' % (
                    path, obj_id, expected_version, (last_contents['version'] if last_contents else '(not stored)')))                
                contents = api.call('doc/read', {'file_id': obj_id})
                code = contents.pop('_code')
                assert code == 'Ok', 'bad code: %r (%r)' % (code, contents.get('_msg'))
                num_changed += 1
                
            # Either way, write the file back, else it will be deleted from disk.
            self._writer.make_data_file(path + '.c.json', data=contents)
            doc_contents[path] = contents

        if num_changed:
            self.logger.info('Found %d documents, %d changed' % (len(doc_contents), num_changed))
        else:
            self.logger.debug('Found %d documents, none changed' % (len(doc_contents), ))
        return doc_contents

    def _make_processed_files(self, doc_contents):
        for path, contents in sorted(doc_contents.items()):
            as_text = _record_to_text(contents)
            self._writer.make_data_file(path + '.txt', contents=as_text)

            
def _iterate_contents(contents):
    """Given a @p contents value (from doc/read API) walk all items in in-order

    @returns iterator over all nodes. Each node is fresly deepcopied, and has extra field:
       _parents -- list of node parent id's
    The return values can be safely modifed at-wil
    """

    to_return = {x['id']: copy.deepcopy(x) for x in contents['nodes']}
    # It is not documented, but looks like top-level node always has an id of 'root'.
    # Let's assume this is the case (and we'd fail if this is not true)
    
    todo = [('root', [])]
    while todo:
        node_id, parents = todo.pop(0)
        data = copy.deepcopy(to_return.pop(node_id))
        data.update(_parents=parents)
        todo = [(child, parents + [node_id])
                for child in data.get('children', ())] + todo
        yield data
        
    assert not to_return, 'found orphaned nodes: %r' % (sorted(to_return.keys()), )

    
def _dict_to_readable(val_dict):
    parts = []
    for k, v in sorted(val_dict.items()):
        if v is True:
            parts.append(k)
            continue
            
        if repr(v) == '"%s"' % str(v) and ',' not in v:
            v_str = str(v)  # Safe string representation
        else:
            v_str = repr(v) # Unsafe string representation
        parts.append('%s=%s' % (k, v_str))
    return ', '.join(parts)


def _record_to_text(contents):
    """Convert a single record to text.
    The text is optimized for git diffs.

    Note that the conversion is intentionally somewhat lossy -- things which
    are not part of content (like collapse status or version numbers) are not included.
    """
    out = io.StringIO()

    meta = copy.copy(contents)
    meta.pop("nodes")
    meta.pop('file_id')
    meta.pop('version')
    print('### FILE: %s' % meta.pop('title'), file=out)
    if meta:
        print('# meta: ' + _dict_to_readable(meta), file=out)

    for node in _iterate_contents(contents):
        spacer = ' ' * (4 * len(node.pop('_parents')))
        node.pop('children', None)   # Do not care
        node.pop('id')
        node.pop('collapsed', None)  # Logically, "collapsed" is not part of content
        note = node.pop('note')
        ctime, mtime = node.pop('created'), node.pop('modified')
        
        # Print content. First line gets '*', the rest get '.'
        for lnum, line in enumerate(node.pop('content').split('\n')):
            print(spacer + ('. ' if lnum else '* ') + line, file=out)

        # Print notes, if we have them.
        if note:
            for line in note.splitlines():
                print(spacer + '_ ' + line, file=out)

        # Finally, the rest
        if node:
            print(spacer + '# ' + _dict_to_readable(node), file=out)
        
        # print(json.dumps(entry, indent=4, sort_keys=True), file=out)

    return out.getvalue()
            

def UNUSED_api_doc_to_flat_record(api_doc, file_entry):
    # Create "writeable" record, optimized for git diffs of moves tracking. Specifically:
    # - Nodes are sorted by key -- this minimizes diffs when hierarchy is moved
    # - Each node gets "parent" field
    # - Record 0 gets meta info
    fs_doc = copy.deepcopy(api_doc['nodes'])
    fs_doc.sort(key=lambda r: r['id'])
    parents = dict()
    for parent in fs_doc:
        for child_id in parent.get("children", []):
            parents[child_id] = parent["id"]
    for v in fs_doc:
        v['parent'] = parents.get(v["id"])        
    file_meta = copy.deepcopy(file_entry)
    file_meta.update(title=api_doc['title'], # get the latest title
                     _meta_record='file/list')
    fs_doc.insert(0, file_meta)
    return fs_doc
    
                    
class UNUSED_DynalistExporter:
    """
    Download all notes from dynalist.io account, and store in datadir.
    
    The folders correspond to direcories; each document generates two files:
      .json  in dynalist-native format
      .opml  in opml format

    opml notes:
    docs: http://dev.opml.org/spec2.html
    view: sudo apt install texlive pandoc
          pandoc FILE.opml -s -o FILE.pdf    
    """

    def __init__(self, datadir):
        self.writer = FileWriter(datadir)


    def _process_one_file(self, api, file_prefix, file_entry):
        api_doc = api.call('doc/read', dict(file_id=file_entry["id"]),)
        assert api_doc.keys() == {'title', 'nodes', 'file_id', 'version', '_code'}, \
            'bad doc keys in file %r: %r' % (file_entry["id"], api_doc.keys())
        assert api_doc["file_id"] ==  file_entry["id"]

        # Practically, API always returns a single top node with id "root", but this is not mentioned
        # in the docs. So make a list of toplevel items, and verify.
        child_ids = set(sum((x.get('children', []) for x in api_doc['nodes']), []))
        toplevels = [x['id'] for x in api_doc['nodes'] if x['id'] not in child_ids]
        assert len(toplevels) == 1, 'got more than one toplevel (%r)' % (sorted(toplevels), )
        toplevel_id = toplevels[0]

        # Create json representation
        todo = {x['id']: copy.deepcopy(x) for x in api_doc['nodes']}
        top = self._nested_record_recurse_children(toplevel_id, todo)
        assert not todo, 'found orphaned nodes: %r' % (sorted(todo.keys()), )
        # For first record, store file entry as well
        top['file_entry'] = copy.deepcopy(file_entry)
        self._writer.make_data_file(file_prefix + '.json', data=top)

        # Create OPML representation
        # (see http://dev.opml.org/spec2.html)

    def _nested_record_recurse_children(self, top_id, todo):
        rec = todo.pop(top_id)
        assert rec, 'child %r not found' % (top_id, )
        rec['z_children'] = [self._nested_record_recurse_children(x, todo)
                             for x in rec.get('children', [])]
        return rec

    
def main():

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-v', '--verbose', action='store_true', help='Print debug messages')
    parser.add_argument(
        '-C', '--cache', action='store_true',
        help='Cache requests and use cache. Returns stale data, but prevents ratelimits '
        'while developing')
    parser.add_argument(
        '--data-dir', metavar='DIR', 
        help='Data directory (must be a git root)')
    parser.add_argument(
        '--dry-run', action='store_true', help='Do not write anything')
    parser.add_argument(
        '--skip-clean', action='store_true', help='Do not delete old files')
    parser.add_argument(
        '--commit', action='store_true', help='Git commit results')
    args = parser.parse_args()

    logging.basicConfig(level=(logging.DEBUG if args.verbose else logging.INFO),
                        format="[%(levelname).1s] %(name)s: %(message)s")

    if args.data_dir:
        data_dir = os.path.expanduser(args.data_dir)
    else:
        for data_dir in DATA_DIRECTORIES:
            if os.path.isdir(data_dir):
                break
        else:
            raise Exception('Cannot find data directories, none of those exist: %r' % (
                DATA_DIRECTORIES, ))
            
    writer = FileWriter(data_dir, dry_run=args.dry_run)
    if args.commit:
        writer.check_git()
        
    downloader = Downloader(writer)
    
    api = DynalistApi(from_cache=args.cache)
    downloader.sync_all(api)

    writer.finalize(delete_others=not args.skip_clean)

    if args.commit:
        writer.git_commit(dry_run=args.dry_run)
        

if __name__ == '__main__':
    main()
