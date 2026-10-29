"""Migration from herdr-spring-load 0.2.0 only. 0.2.0 added private keybindings
to herdr's config (a managed [[keys.command]] block and ctrl+alt+shift focus
aliases) and kept a private signing keychain. 0.3.0 needs neither, so setup and
uninstall take them out again, restoring the config as 0.2.0 recorded it in
state.json. Nothing here runs for a clean install."""
import json
import os
import re
import tomllib

from installer import checked, run, tool

BEGIN = '# BEGIN herdr-spring-load managed binding'
END = '# END herdr-spring-load managed binding'
FOCUS = {'left':('f8','prefix+h'), 'right':('f9','prefix+l'), 'up':('f10','prefix+k'), 'down':('f11','prefix+j')}
BINDING = BEGIN+'\n'+''.join(f'''[[keys.command]]
key = "ctrl+alt+shift+f{i}"
type = "plugin_action"
command = "herdr-spring-load.probe-{i}"
description = "herdr-spring-load client probe {i}"
''' for i in range(1,8))+END+'\n'

def strip_block(text):
    """Remove the managed block and the one blank-line separator setup put before it."""
    return re.sub(r'\n?'+re.escape(BEGIN)+r'.*?'+re.escape(END)+r'\n?', '', text, flags=re.S)


def canonical(chord):
    # Same modifier aliases as herdr's parse_modifier_token.
    aliases = {'control':'ctrl','option':'alt','meta':'alt','command':'cmd','super':'cmd'}
    parts = [aliases.get(p.strip().lower(),p.strip().lower()) for p in chord.split('+')]
    return frozenset(parts)

def chord_values(value):
    if isinstance(value,str): return [value] if value else []
    if isinstance(value,list) and all(isinstance(v,str) for v in value): return value
    raise ValueError('shortcut must be a string or an array of strings')

def parses_as_value(fragment):
    try:
        tomllib.loads('v = '+fragment)
        return True
    except tomllib.TOMLDecodeError:
        return False

def rewrite_focus(text, parsed, remove=False, drop=(), drop_table=False, originals=None):
    """Add the private focus aliases, or remove them. On removal, fields named in
    `drop` (added by setup) are deleted when only their default is left, and with
    `drop_table` an emptied [keys] table that setup created goes too."""
    keys = parsed.get('keys',{})
    headings = list(re.finditer(r'(?m)^[ \t]*(\[\[?)([^\]\n]+)\]\]?[ \t]*(?:#[^\n]*)?$',text))
    table = next((h for h in headings if h.group(1)=='[' and h.group(2).strip().strip('"\'')=='keys'),None)
    edits=[]
    inserts=[]
    for direction,(key,default) in FOCUS.items():
        field='focus_pane_'+direction
        private='ctrl+alt+shift+'+key
        original=chord_values(keys.get(field,default))
        values=[v for v in original if canonical(v)!=canonical(private)]
        if not remove: values.append(private)
        if remove and values==original: continue
        value=json.dumps(values)
        if field in keys:
            if not table: raise ValueError('use an explicit [keys] table for focus shortcuts before setup')
            finish=next((h.start() for h in headings if h.start()>table.start()),len(text))
            segment=text[table.end():finish]
            match=re.search(r'(?m)^[ \t]*'+field+r'[ \t]*=[ \t]*',segment)
            if not match: raise ValueError('cannot safely edit focus shortcuts; use normal [keys] assignments')
            start=table.end()+match.end()
            # Read complete TOML value, including multiline arrays, before replacing.
            end=start
            while True:
                newline=text.find('\n',end)
                end=len(text) if newline<0 else newline
                try:
                    tomllib.loads('v = '+text[start:end])
                    break
                except tomllib.TOMLDecodeError:
                    if end==len(text): raise ValueError('invalid focus shortcut value')
                    end+=1
            # Keep trailing blanks and an inline comment: replace the value only.
            end=next(k for k in range(start+1,end+1)
                     if re.fullmatch(r'[ \t]*(#.*)?',text[k:end]) and parses_as_value(text[start:k]))
            if remove and field in drop and values==[default]:
                # Setup created this line: take it out, so herdr's default applies again.
                line=text.rfind('\n',0,table.end()+match.start())+1
                edits.append((line,min(len(text),end+1),''))
            else:
                original_value=(originals or {}).get(field)
                if remove and original_value is not None and chord_values(original_value)==values:
                    value=json.dumps(original_value) # the user's own form: a string stays a string
                edits.append((start,end,value))
        elif not remove:
            inserts.append(field+' = '+value+'\n')
    if inserts:
        if table:
            at=next((h.start() for h in headings if h.start()>table.start()),len(text))
            edits.append((at,at,('\n' if at and text[at-1]!='\n' else '')+''.join(inserts)))
        else:
            # A new table must follow the top-level keys, or it would capture them.
            at=headings[0].start() if headings else len(text)
            edits.append((at,at,('\n' if at and text[at-1]!='\n' else '')+'[keys]\n'+''.join(inserts)))
    for start,end,value in sorted(edits,reverse=True): text=text[:start]+value+text[end:]
    if remove and drop_table:
        headings = list(re.finditer(r'(?m)^[ \t]*(\[\[?)([^\]\n]+)\]\]?[ \t]*(?:#[^\n]*)?\n?',text))
        table = next((h for h in headings if h.group(1)=='[' and h.group(2).strip().strip('"\'')=='keys'),None)
        if table:
            finish=next((h.start() for h in headings if h.start()>table.start()),len(text))
            if not text[table.end():finish].strip():
                text=text[:table.start()]+text[finish:]
    return text


def unprepare_config(text, state):
    """Undo 0.2.0's prepare_config using what it recorded at install (state.json);
    without a record, strip the block and the private aliases only."""
    without = strip_block(text)
    if state and any(k in state for k in ('focus_fields_added', 'keys_table_added', 'focus_originals')):
        result = rewrite_focus(without, tomllib.loads(without), remove=True,
                               drop=set(state.get('focus_fields_added', [])), drop_table=state.get('keys_table_added', False),
                               originals=state.get('focus_originals', {}))
    else:
        result = rewrite_focus(without, tomllib.loads(without), remove=True)
    tomllib.loads(result)
    return result

def has_bindings(text):
    return BEGIN in text or any('ctrl+alt+shift+'+key in text for key, _ in FOCUS.values())

def restored_config(config, state):
    """(before, after) for the config file, or None when 0.2.0 left nothing in it."""
    if not config.exists():
        return None
    before = config.read_text()
    if not has_bindings(before):
        return None
    after = unprepare_config(before, state)
    return None if after == before else (before, after)

LEGACY_FILES = ('herdr-spring-load', 'lib/setup.py', 'lib/probe.py', 'lib/uninstall.py')

def reload_sessions(config_dir, request):
    """Ask every running session to re-read the restored config, so the private
    keybindings go away without a restart. A session without a server reads it
    when it starts."""
    sockets = [config_dir/'herdr.sock']
    sessions = config_dir/'sessions'
    if sessions.is_dir():
        sockets += sorted(p/'herdr.sock' for p in sessions.iterdir() if p.is_dir())
    reloaded = []
    for socket_path in sockets:
        if not socket_path.exists():
            continue
        try:
            request(str(socket_path), 'server.reload_config', {}, timeout=5)
            reloaded.append(socket_path)
        except (OSError, ValueError, RuntimeError):
            pass   # no server there, or it refused: it reads the file when it starts
    return reloaded

def keychain_list(security):
    out = checked([security, 'list-keychains', '-d', 'user'], 'reading the keychain search list')
    return [line.strip()[1:-1] if line.strip().startswith('"') and line.strip().endswith('"') else line.strip()
            for line in out.splitlines() if line.strip()]

def same_file(a, b):
    return os.path.realpath(a) == os.path.realpath(b)

def drop_from_search_list(security, keychain):
    """Take our private keychain out of the user search list, keeping every other
    entry exactly as it is now (re-read, never a stale copy)."""
    current = keychain_list(security)
    rest = [k for k in current if not same_file(k, keychain)]
    if rest != current:
        checked([security, 'list-keychains', '-d', 'user', '-s', *rest], 'restoring the keychain search list')

def delete_keychain(security, keychain):
    if keychain.exists():
        run([security, 'delete-keychain', str(keychain)]) # also removes it from the search list
    keychain.unlink(missing_ok=True)
    drop_from_search_list(security, keychain)

def remove_signing(support):
    """Delete 0.2.0's private signing keychain (never the login keychain)."""
    keychain = support/'signing/signing.keychain-db'
    folder = keychain.parent
    if not folder.exists():
        return False
    if keychain.exists():
        delete_keychain(tool('security'), keychain)
    for name in ('keychain-password',):
        (folder/name).unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass
    return True
