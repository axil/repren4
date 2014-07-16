#!/usr/bin/env python

r'''
Repren is a simple but flexible command-line tool for rewriting file contents
according to a set of regular expression patterns, and to rename or move files
according to patterns. Essentially, it is a general-purpose, brute-force text
file refactoring tool. For example, repren could rename all occurrences of
certain class and variable names a set of Java source files, while
simultaneously renaming the Java files according to the same pattern. It's more
powerful than usual options like `perl -pie`, `rpl`, or `sed`:

- It can also rename files, including moving files and creating directories.
- It performs group renamings (e.g. rename "foo" as "bar", and "bar" as "foo"
  at once, without requiring a temporary intermediate rename).
- It supports "magic" case-preserving renames that let you find and rename
  identifiers with case variants (lowerCamel, UpperCamel, lower_underscore, and
  UPPER_UNDERSCORE) consistently.
- It has a nondestructive mode, prints stats on its changes, and has a number
  of other useful options (see usage).
- It has this nice help page!

If file paths are provided, repren replaces those files in place, leaving a
backup with extension ".orig". If directory paths are provided, it applies
replacements recursively to all files in the supplied paths that are not in the
exclude pattern. If no arguments are supplied, it reads from stdin and writes
to stdout.

Patterns must be supplied in a text file, of the form <regex><tab><replacement>,
one per line. Empty lines and #-prefixed comments are OK.

Examples (here `patfile` is a patterns file):

    # Rewrite stdin:
    repren -p patfile < input > output

    # Rewrite a few files in place, also requiring matches be on word breaks:
    repren -p patfile --word-breaks myfile1 myfile2 myfile3

    # Rewrite whole directory trees. Since this is a big operation, we use
    # `-n` to do a dry run that only prints what would be done:
    repren -n -p patfile --word-breaks --full mydir1

    # Now actually do it:
    repren -p patfile --word-breaks --full mydir1

    # Same as above, for all case variants:
    repren -p patfile --word-breaks --preserve-case --full mydir1

Notes:

- As with sed, replacements are made line by line by default. Memory
  permitting, replacements may be done on entire files using `--at-once`.
- As with sed, replacement text may include backreferences to groups within the
  regular expression, using the usual syntax: \1, \2, etc.
- In the pattern file, both the regular expression and the replacement may
  contain the usual escapes `\n`, `\t`, etc. (To match a multi-line pattern,
  containing `\n`, you must must use `--at-once`.)
- Replacements are all matched on each input file, then all replaced, so it's
  possible to swap or otherwise change names in ways that would require
  multiple steps if done one replacement at at a time.
- If two patterns have matches that overlap, only one replacement is applied,
  with preference to the pattern appearing first in the patterns file.
- If one pattern is a subset of another, consider if `--word-breaks` will help.
- If patterns have special charaters, `--literal` may help.
- The case-preserving option works by adding all case variants to the pattern
  replacements, e.g. if the pattern file has foo_bar -> xxx_yyy, the
  replacements fooBar -> xxxYYY, FooBar -> XxxYyy, FOO_BAR -> XXX_YYY are also
  made. Assumes each pattern has one casing convention. (Plain ASCII names only.)
- The same logic applies to filenames, with patterns applied to the full file
  path with slashes replaced and then and parent directories created as needed,
  e.g. `my/path/to/filename` can be rewritten to `my/other/path/to/otherfile`.
  (Use caution and test with `-n`, especially when using absolute path
  arguments!)
- Files are never clobbered by renames. If a target already exists, or multiple
  files are renamed to the same target, numeric suffixes will be added to make
  the files distinct (".1", ".2", etc.).
- Files are created at a temporary location, then renamed, so original files are
  left intact in case of unexpected errors. File permissions are preserved.
- Backups are created of all modified files, with the suffix ".orig".
- By default, recursive searching omits paths starting with ".". This may be
  adjusted with `--exclude`. Files ending in `.orig` are always ignored.
- Data can be in any encoding, as it is treated as binary, and not interpreted
  in a specific encoding like UTF-8. This is less error prone in real-life
  situations where files have encoding inconsistencies. However, note the
  `--case-preserving` logic only handles casing conversions correctly for plain
  ASCII letters `[a-zA-Z]`.
'''

# Author: jlevy
# Created: 2014-07-09

from __future__ import print_function
import re, sys, os, shutil, optparse, bisect

VERSION = "0.3.0"
DESCRIPTION = "repren: Multi-pattern string replacement and file renaming"

BACKUP_SUFFIX = ".orig"
TEMP_SUFFIX = ".repren.tmp"
DEFAULT_EXCLUDE_PAT = r"\."

def log(op, msg):
  if op:
    msg = "- %s: %s" % (op, msg)
  print(msg, file=sys.stderr)

def fail(msg):
  print("error: " + msg, file=sys.stderr)
  sys.exit(1)

class _Tally:
  def __init__(self):
    self.files = 0
    self.chars = 0
    self.matches = 0
    self.valid_matches = 0
    self.files_written = 0
    self.renames = 0

global _tally
_tally = _Tally()


## String matching

def _overlap(match1, match2):
  return match1.start() < match2.end() and match2.start() < match1.end()

def _sort_drop_overlaps(matches, source_name=None):
  '''Select and sort a set of disjoint intervals, omitting ones that overlap.'''
  non_overlaps = []
  starts = []
  for (match, replacement) in matches:
    index = bisect.bisect_left(starts, match.start())
    if index > 0:
      (prev_match, _) = non_overlaps[index - 1]
      if _overlap(prev_match, match):
        log(source_name, "Skipping overlapping match '%s' of '%s' that overlaps '%s' of '%s' on its left" %
          (match.group(), match.re.pattern, prev_match.group(), prev_match.re.pattern))
        continue
    if index < len(non_overlaps):
      (next_match, _) = non_overlaps[index]
      if _overlap(next_match, match):
        log(source_name, "Skipping overlapping match '%s' of '%s' that overlaps '%s' of '%s' on its right" %
          (match.group(), match.re.pattern, next_match.group(), next_match.re.pattern))
        continue
    starts.insert(index, match.start())
    non_overlaps.insert(index, (match, replacement))
  return non_overlaps

def _apply_replacements(input, matches):
  out = []
  pos = 0
  for (match, replacement) in matches:
    out.append(input[pos:match.start()])
    out.append(match.expand(replacement))
    pos = match.end()
  out.append(input[pos:])
  return "".join(out)

class _MatchCounts:
  def __init__(self, found=0, valid=0):
    self.found = found
    self.valid = valid

  def add(self, o):
    self.found += o.found
    self.valid += o.valid

def multi_replace(input, patterns, is_path=False, source_name=None):
  '''Replace all occurrences in the input given a list of patterns (regex,
  replacement), simultaneously, so that no replacement affects any other. E.g.
  { xxx -> yyy, yyy -> xxx } or { xxx -> yyy, y -> z } are possible. If matches
  overlap, one is selected, with matches appearing earlier in the list of
  patterns preferred.
  '''
  matches = []
  for (regex, replacement) in patterns:
    for match in regex.finditer(input):
      matches.append((match, replacement))
  valid_matches = _sort_drop_overlaps(matches, source_name=source_name)
  result = _apply_replacements(input, valid_matches)

  global _tally
  if not is_path:
    _tally.chars += len(input)
    _tally.matches += len(matches)
    _tally.valid_matches += len(valid_matches)

  return (result, _MatchCounts(len(matches), len(valid_matches)))


## Case handling (only used for case-preserving magic)

# TODO: Could handle dash-separated names as well.

# FooBarBaz -> Foo, Bar, Baz
# XMLFooHTTPBar -> XML, Foo, HTTP, Bar
_camel_split_pat1 = re.compile("([^A-Z])([A-Z])")
_camel_split_pat2 = re.compile("([A-Z])([A-Z][^A-Z])")

_name_pat = re.compile(r"\w+")

def _split_name(name):
  '''Split a camel-case or underscore-formatted name into words. Return separator and words.'''
  if name.find("_") >= 0:
    return ("_", name.split("_"))
  else:
    temp = _camel_split_pat1.sub("\\1\t\\2", name)
    temp = _camel_split_pat2.sub("\\1\t\\2", temp)
    return ("", temp.split("\t"))

def _capitalize(word):
  return word[0].upper() + word[1:].lower()

def to_lower_camel(name):
  words = _split_name(name)[1]
  return words[0].lower() + "".join([_capitalize(word) for word in words[1:]])

def to_upper_camel(name):
  words = _split_name(name)[1]
  return "".join([_capitalize(word) for word in words])

def to_lower_underscore(name):
  words = _split_name(name)[1]
  return "_".join([word.lower() for word in words])

def to_upper_underscore(name):
  words = _split_name(name)[1]
  return "_".join([word.upper() for word in words])

def _transform_expr(expr, transform):
  return _name_pat.sub(lambda m: transform(m.group()), expr)

def all_case_variants(expr):
  '''Return all casing variations of an expression, replacing each name with
  lower- and upper-case camel-case and underscore style names, in fixed order.'''
  return [_transform_expr(expr, transform) for transform in [to_lower_camel, to_upper_camel, to_lower_underscore, to_upper_underscore]]


## File handling

def make_parent_dirs(path):
  '''Ensure parent directories of a file are created as needed.'''
  dir = os.path.dirname(path)
  if dir and not os.path.isdir(dir):
    os.makedirs(dir)
  return path

def move_file(source_path, dest_path, clobber=False):
   if not clobber:
     trailing_num = re.compile("(.*)[.]\d+$")
     i = 1
     while os.path.exists(dest_path):
       match = trailing_num.match(dest_path)
       if match:
         dest_path = match.group(1)
       dest_path = "%s.%s" % (dest_path, i)
       i = i + 1
   shutil.move(source_path, dest_path)

def transform_stream(transform, input, output, by_line=False):
  counts = _MatchCounts()
  if by_line:
    for line in input:
      if transform:
        (new_line, new_counts) = transform(line)
        counts.add(new_counts)
      else:
        new_line = new_line
      output.write(new_line)
  else:
    contents = input.read()
    (new_contents, new_counts) = transform(contents) if transform else contents
    output.write(new_contents)
  return counts

def transform_file(transform, source_path, dest_path, orig_suffix=BACKUP_SUFFIX, temp_suffix=TEMP_SUFFIX, by_line=False, dry_run=False):
  '''Transform full contents of file at source_path in memory with specified
  function, writing dest_path atomically and keeping a backup. Source and
  destination may be the same path.'''
  counts = _MatchCounts()
  if transform:
    orig_path = source_path + orig_suffix
    temp_path = dest_path + temp_suffix
    make_parent_dirs(temp_path)
    perms = os.stat(source_path).st_mode & 0o777
    with open(source_path, "rb") as input:
      with os.fdopen(os.open(temp_path, os.O_WRONLY | os.O_CREAT, perms), "wb") as output:
        counts = transform_stream(transform, input, output, by_line=by_line)

    # Important: We don't modify original file until the above succeeds without exceptions.
    if not dry_run and counts.found > 0:
      move_file(source_path, orig_path, clobber=True)
      move_file(temp_path, dest_path, clobber=False)
    else:
      os.remove(temp_path)

    global _tally
    _tally.files += 1
    if counts.found > 0:
      _tally.files_written += 1
      if dest_path != source_path:
        _tally.renames += 1

  return counts

def rewrite_file(path, patterns, do_renames=False, do_contents=False, by_line=False, dry_run=False):
  dest_path = multi_replace(path, patterns, is_path=True)[0] if do_renames else path
  transform = None
  counts = _MatchCounts()
  if do_contents:
    transform = lambda contents: multi_replace(contents, patterns, source_name=path)
  counts = transform_file(transform, path, dest_path, by_line=by_line, dry_run=dry_run)
  if counts.found > 0:
    log("modify", "%s: %s matches" % (path, counts.found))
  if dest_path != path:
    log("rename", "%s -> %s" % (path, dest_path))


def walk_files(paths, exclude_pat=DEFAULT_EXCLUDE_PAT):
  out = []
  exclude_re = re.compile(exclude_pat)
  for path in paths:
    if not os.path.exists(path):
      fail("path not found: %s" % path)
    if os.path.isfile(path):
      out.append(path)
    else:
      for (root, dirs, files) in os.walk(path):
        # Prune files that are excluded, and always prune backup files.
        out += [os.path.join(root, f) for f in files if not exclude_re.match(f) and not f.endswith(BACKUP_SUFFIX) and not f.endswith(TEMP_SUFFIX)]
        # Prune subdirectories.
        dirs[:] = [d for d in dirs if not exclude_re.match(d)]
  return out

def rewrite_files(root_paths, patterns, do_renames=False, do_contents=False, exclude_pat=DEFAULT_EXCLUDE_PAT, by_line=False, dry_run=False):
  paths = walk_files(root_paths, exclude_pat=exclude_pat)
  log(None, "Found %s files in: %s" % (len(paths), ", ".join(root_paths)))
  for path in paths:
    rewrite_file(path, patterns, do_renames=do_renames, do_contents=do_contents, by_line=by_line, dry_run=dry_run)


## Invocation

def parse_patterns(patterns_str, literal=False, word_breaks=False, insensitive=False, preserve_case=False):
  patterns = []
  flags = re.I if insensitive else 0
  for line in patterns_str.splitlines():
    try:
      bits = line.split('\t')
      if line.strip().startswith("#"):
        continue
      elif line.strip() and len(bits) == 2:
        (regex, replacement) = bits
        if literal:
          regex = re.escape(regex)
        pairs = []
        if preserve_case:
          pairs += zip(all_case_variants(regex), all_case_variants(replacement))
        pairs.append((regex, replacement))
        # Avoid spurious overlap warnings by removing dups.
        pairs = sorted(set(pairs))
        for (regex_variant, replacement_variant) in pairs:
          if word_breaks:
            regex_variant = r'\b' + regex_variant + r'\b'
          patterns.append((re.compile(regex_variant, flags), replacement_variant))
      else:
        fail("invalid line in pattern file: %s" % bits)
    except Exception as e:
      raise e
      fail("error parsing pattern: %s: %s" % (e, bits))
  return patterns

# Remove excessive epilog formatting in optparse.
optparse.OptionParser.format_epilog = lambda self, formatter: self.epilog

if __name__ == '__main__':
  USAGE = "%prog -p <pattern-file> [options] [path ...]"
  parser = optparse.OptionParser(usage=USAGE, description=DESCRIPTION, epilog="\n" + __doc__, version=VERSION)
  parser.add_option("-p", "--patterns", help = "file with replacement patterns (see below)", dest = "patterns")
  parser.add_option("-F", "--full", help = "do file renames and search/replace on file contents", dest = "do_full", action = "store_true")
  parser.add_option("-f", "--renames", help = "do file renames only; do not modify file contents", dest = "do_renames", action = "store_true")
  parser.add_option("-l", "--literal", help = "use exact string matching, rather than regular expresion matching", dest = "literal", action = "store_true")
  parser.add_option("-i", "--insensitive", help = "match case-insensitively", dest = "insensitive", action = "store_true")
  parser.add_option("-c", "--preserve-case", help = "do case-preserving magic to transform all case variants (see below)", dest = "preserve_case", action = "store_true")
  parser.add_option("-b", "--word-breaks", help = "require word breaks (regex \\b) around all matches", dest = "word_breaks", action = "store_true")
  parser.add_option("--exclude", help = "file/directory name regex to exclude", dest = "exclude_pat", default = DEFAULT_EXCLUDE_PAT)
  parser.add_option("--at-once", help = "transform each file's contents at once, instead of line by line", dest = "at_once", action = "store_true")
  parser.add_option("-t", "--parse-only", help = "parse and show patterns only", dest = "parse_only", action = "store_true")
  parser.add_option("-n", "--dry-run", help = "dry run: just log matches without changing files", dest = "dry_run", action = "store_true")

  (options, root_paths) = parser.parse_args()

  if options.dry_run:
    log(None, "Dry run: No files will be changed")

  options.do_contents = not options.do_renames
  options.do_renames = options.do_renames or options.do_full

  # log(None, "Settings: %s" % options)

  if not options.patterns:
    parser.error("pattern file is required")
  if options.insensitive and options.preserve_case:
    parser.error("cannot use --insensitive and --preserve-case at once")

  by_line = not options.at_once

  with open(options.patterns, "rb") as f:
    patterns = parse_patterns(f.read(), literal=options.literal, word_breaks=options.word_breaks, insensitive=options.insensitive, preserve_case=options.preserve_case)

  if len(patterns) == 0:
    fail("found no parse patterns")

  log(None, ("Using %s patterns:\n  " % len(patterns)) + "\n  ".join(["'%s' -> '%s'" % (regex.pattern, replacement) for (regex, replacement) in patterns]))

  if not options.parse_only:
    if len(root_paths) > 0:
      rewrite_files(root_paths, patterns, do_renames=options.do_renames, do_contents=options.do_contents, by_line=by_line, dry_run=options.dry_run)

      log(None, "Read %s files (%s chars), found %s matches (%s skipped due to overlaps)" % (_tally.files, _tally.chars, _tally.valid_matches, _tally.matches - _tally.valid_matches))
      change_words = "Dry run: Would have changed" if options.dry_run else "Changed"
      log(None, "%s %s files, including %s renames" % (change_words, _tally.files_written, _tally.renames))
    else:
      if options.do_renames:
        parser.error("can't specify --renames on stdin; give filename arguments")
      if options.dry_run:
        parser.error("can't specify --dry-run on stdin; give filename arguments")
      transform = lambda contents: multi_replace(contents, patterns)
      transform_stream(transform, sys.stdin, sys.stdout, by_line=by_line)

      log(None, "Read %s chars, made %s replacements (%s skipped due to overlaps)" % (_tally.chars, _tally.valid_matches, _tally.matches - _tally.valid_matches))


# TODO:
#   --undo mode to revert a previous run by using .orig files; --clean mode to remove .orig files
#   Expose re.MULTILINE flag
#   Log collisions
#   Separate patterns file for renames and replacements
#   Quiet and verbose modes (the latter logging each substitution)
#   Supply patterns directly on command line
#   Support --preserve-case for Unicode (non-ASCII) characters (messy)
