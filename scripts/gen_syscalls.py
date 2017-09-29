#!/usr/bin/env python3
#
# Copyright (c) 2017 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0

import sys
import re
import argparse
import os


api_regex = re.compile(r'''
__(syscall|syscall_inline)\s+   # __syscall or __syscall_inline
([^(]+)                         # type and name of system call (split later)
[(]                             # Function opening parenthesis
([^)]*)                         # Arg list (split later)
[)]                             # Closing parenthesis
''', re.MULTILINE | re.VERBOSE)

typename_regex = re.compile(r'(.*?)([A-Za-z0-9_]+)$')

class SyscallParseException(Exception):
    pass


def typename_split(item):
    if "[" in item:
        raise SyscallParseException("Please pass arrays to syscalls as pointers, unable to process '%s'"
                % item)

    if "(" in item:
        raise SyscallParseException("Please use typedefs for function pointers")

    mo = typename_regex.match(item)
    if not mo:
        raise SyscallParseException("Malformed system call invocation")

    m = mo.groups()
    return (m[0].strip(), m[1])


def analyze_fn(match_group, fn):
    variant, func, args = match_group

    try:
        if args == "void":
            args = []
        else:
            args = [typename_split(a.strip()) for a in args.split(",")]

        func_type, func_name = typename_split(func)
    except SyscallParseException:
        sys.stderr.write("In declaration of %s\n" % func)
        raise

    sys_id = "K_SYSCALL_" + func_name.upper()
    is_void = (func_type == "void")

    # Get the proper system call macro invocation, which depends on the
    # number of arguments, the return type, and whether the implementation
    # is an inline function
    macro = "K_SYSCALL_DECLARE%d%s%s" % (len(args),
            "_VOID" if is_void else "",
            "_INLINE" if variant == "syscall_inline" else "")

    # Flatten the argument lists and generate a comma separated list
    # of t0, p0, t1, p1, ... tN, pN as expected by the macros
    flat_args = [i for sublist in args for i in sublist]
    if not is_void:
        flat_args = [func_type] + flat_args
    flat_args = [sys_id, func_name] + flat_args
    argslist = ", ".join(flat_args)

    invocation = "%s(%s);" % (macro, argslist)

    handler = "_handler_" + func_name

    # Entry in _k_syscall_table
    table_entry = "[%s] = %s" % (sys_id, handler)

    return (fn, handler, invocation, sys_id, table_entry)


def analyze_headers(base_path):
    ret = []

    for root, dirs, files in os.walk(base_path):
        for fn in files:

            # toolchain/common.h has the definition of __syscall which we
            # don't want to trip over
            path = os.path.join(root, fn)
            if not fn.endswith(".h") or path.endswith("toolchain/common.h"):
                continue

            with open(path, "r") as fp:
                try:
                    result = [analyze_fn(mo.groups(), fn)
                              for mo in api_regex.finditer(fp.read())]
                except Exception:
                    sys.stderr.write("While parsing %s\n" % fn)
                    raise

            ret.extend(result)

    return ret

table_template = """/* auto-generated by gen_syscalls.py, don't edit */

/* Weak handler functions that get replaced by the real ones unles a system
 * call is not implemented due to kernel configuration
 */
%s

const _k_syscall_handler_t _k_syscall_table[K_SYSCALL_LIMIT] = {
\t%s
};
"""

list_template = """
/* auto-generated by gen_syscalls.py, don't edit */
#ifndef _ZEPHYR_SYSCALL_LIST_H_
#define _ZEPHYR_SYSCALL_LIST_H_

#ifndef _ASMLANGUAGE

#ifdef __cplusplus
extern "C" {
#endif

enum {
\t%s
};

%s

#ifdef __cplusplus
}
#endif

#endif /* _ASMLANGUAGE */

#endif /* _ZEPHYR_SYSCALL_LIST_H_ */
"""

syscall_template = """
/* auto-generated by gen_syscalls.py, don't edit */

#ifndef _ASMLANGUAGE

#include <syscall_list.h>
#include <syscall_macros.h>

#ifdef __cplusplus
extern "C" {
#endif

%s

#ifdef __cplusplus
}
#endif

#endif
"""

handler_template = """
extern u32_t %s(u32_t arg1, u32_t arg2, u32_t arg3,
                u32_t arg4, u32_t arg5, u32_t arg6, void *ssf);
"""

weak_template = """
__weak ALIAS_OF(_handler_no_syscall)
u32_t %s(u32_t arg1, u32_t arg2, u32_t arg3,
         u32_t arg4, u32_t arg5, u32_t arg6, void *ssf);
"""


def parse_args():
    global args
    parser = argparse.ArgumentParser(description = __doc__,
            formatter_class = argparse.RawDescriptionHelpFormatter)

    parser.add_argument("-i", "--include", required=True,
            help="Base include directory")
    parser.add_argument("-d", "--syscall-dispatch", required=True,
            help="output C system call dispatch table file")
    parser.add_argument("-o", "--base-output", required=True,
            help="Base output directory for syscall macro headers")
    args = parser.parse_args()


def main():
    parse_args()

    syscalls = analyze_headers(args.include)
    invocations = {}
    ids = []
    table_entries = []
    handlers = []

    for fn, handler, inv, sys_id, entry in syscalls:
        if fn not in invocations:
            invocations[fn] = []

        invocations[fn].append(inv)
        ids.append(sys_id)
        table_entries.append(entry)
        handlers.append(handler)

    with open(args.syscall_dispatch, "w") as fp:
        table_entries.append("[K_SYSCALL_BAD] = _handler_bad_syscall")

        weak_defines = "".join([weak_template % name for name in handlers])

        fp.write(table_template % (weak_defines, ",\n\t".join(table_entries)))

    # Listing header emitted to stdout
    ids.sort()
    ids.extend(["K_SYSCALL_BAD", "K_SYSCALL_LIMIT"])
    handler_defines = "".join([handler_template % name for name in handlers])
    sys.stdout.write(list_template % (",\n\t".join(ids), handler_defines))

    os.makedirs(args.base_output, exist_ok=True)
    for fn, invo_list in invocations.items():
        out_fn = os.path.join(args.base_output, fn)

        header = syscall_template % "\n\n".join(invo_list)

        # Check if the file already exists, and if there are no changes,
        # don't touch it since that will force an incremental rebuild
        if os.path.exists(out_fn):
            with open(out_fn, "r") as fp:
                old_data = fp.read()

            if old_data == header:
                continue

        with open(out_fn, "w") as fp:
            fp.write(header)

if __name__ == "__main__":
    main()

