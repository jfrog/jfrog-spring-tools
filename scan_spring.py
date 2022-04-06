import os
import sys
from collections import namedtuple
from enum import Enum, IntEnum
from zipfile import BadZipFile, ZipFile
from tarfile import open as tar_open
from tarfile import CompressionError, ReadError
import zlib
from io import BytesIO
import struct
from jawa.classloader import ClassFile

RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RESET_ALL = "\x1b[0m"

ZIP_EXTENSIONS = {".jar", ".war", ".sar", ".ear", ".par", ".zip", ".apk"}
TAR_EXTENSIONS = {".tar.gz", ".tar"}

ANNOTATION_STRS = {
    b"annotation/RequestMapping;",
    b"annotation/GetMapping",
    b"annotation/PostMapping",
    b"annotation/PutMapping",
    b"annotation/DeleteMapping",
    b"annotation/PatchMapping",
}

ANNOTATION_STRSs = {s.decode("utf-8") for s in ANNOTATION_STRS}

ALLOWED_TYPES = {
    "java/lang/String",
    "boolean",
    "long",
    "int",
    "java/lang/CharSequence",
    "java/lang/Number",
    "java/time/temporal/Temporal",
    "java/time/DateTime",
    "java/util/ArrayList",
    "java/util/Locale",
    "java/net/URI",
    "java/net/URL",
    "org/springframework/web/context/request/NativeWebRequest",
    "org/springframework/web/context/request/WebRequest",
    "javax/servlet/http/HttpServletResponse",
    "javax/servlet/http/HttpServletRequest",
    "javax/servlet/http/HttpSession",
    "javax/servlet/http/PushBuilder",
    "java/security/Principal",
    "org/springframework/http/HttpMethod",
    "java/time/TimeZone",
    "java/time/ZoneId",
    "java/io/InputStream",
    "java/io/OutputStream",
    "java/io/Reader",
    "java/io/Writer",
    "java/util/Map",
    "org/springframework/ui/Model",
    "org/springframework/ui/ModelMap",

}  # TO BE UPDATED

CLASSES_EXEMPTLIST = {"org/springframework/boot"}


def get_annotation_constants(c):
    for const in c.constants:
        if hasattr(const, "value") and isinstance(const.value, str):
            if any(
                const.value.endswith(substr) or const.value.endswith(substr + ";")
                for substr in ANNOTATION_STRSs
            ):
                yield const.index


def check_method_annotations(c, req_constants):
    for method in c.methods:
        for attr in method.attributes:
            if attr.name == "RuntimeVisibleAnnotations":
                index = struct.unpack(">H", attr.info[2:4])[0]
                if index in req_constants and method.args:
                    yield (method.name.value, {arg.name for arg in method.args})


def examine_class(rel_path, file_name, content, silent_mode):
    try:
        cl = ClassFile(BytesIO(content))
    except:  # IndexError, but I don't trust jawa not to throw someting else
        if not silent_mode:
            print("Could not open class: %s" % file_name)
        return

    annotation_constants = get_annotation_constants(cl)

    if not annotation_constants:
        return

    for method_name, arg_type_names in check_method_annotations(
        cl, annotation_constants
    ):
        bad_arg_type_names = [
            arg_type_name
            for arg_type_name in arg_type_names
            if arg_type_name not in ALLOWED_TYPES
        ]
        if bad_arg_type_names:
            print(
                "In %s/%s%s%s: endpoint method %s%s%s accepts %s\n\n"
                % (
                    rel_path,
                    RED,
                    file_name,
                    RESET_ALL,
                    RED,
                    method_name,
                    RESET_ALL,
                    ", ".join(bad_arg_type_names),
                )
            )


def zip_file(file, rel_path: str, silent_mode: bool):
    try:
        with ZipFile(file) as jarfile:
            for file_name in jarfile.namelist():
                if acceptable_filename(file_name):
                    next_file = jarfile.open(file_name, "r")
                    test_file(next_file, os.path.join(rel_path, file_name), silent_mode)
                    continue
                if (
                    file_name.endswith(".class")
                    and not file_name.endswith("module-info.class")
                    and not any(
                        file_name.startswith(exempt_class)
                        for exempt_class in CLASSES_EXEMPTLIST
                    )
                ):
                    content = jarfile.read(file_name)
                    if any(substr in content for substr in ANNOTATION_STRS):
                        examine_class(rel_path, file_name, content, silent_mode)

            # went over all the files in the current layer; draw conclusions
    except (IOError, BadZipFile, UnicodeDecodeError, zlib.error, RuntimeError) as e:
        if not silent_mode:
            print(rel_path + ": " + str(e))
        return


def tar_file(file, rel_path: str, silent_mode: bool):
    try:
        with tar_open(fileobj=file) as tarfile:
            for item in tarfile.getmembers():
                if "../" in item.name:
                    continue
                if item.isfile() and acceptable_filename(item.name):
                    fileobj = tarfile.extractfile(item)
                    new_path = rel_path + "/" + item.name
                    test_file(fileobj, new_path, silent_mode)

    except (
        IOError,
        FileExistsError,
        CompressionError,
        ReadError,
        RuntimeError,
        UnicodeDecodeError,
        zlib.error,
    ) as e:
        if not silent_mode:
            print(rel_path + ": " + str(e))
        return


def test_file(file, rel_path: str, silent_mode: bool):
    if any(rel_path.endswith(ext) for ext in ZIP_EXTENSIONS):
        zip_file(file, rel_path, silent_mode)

    elif any(rel_path.endswith(ext) for ext in TAR_EXTENSIONS):
        tar_file(file, rel_path, silent_mode)


def acceptable_filename(filename: str):
    return any(filename.endswith(ext) for ext in ZIP_EXTENSIONS | TAR_EXTENSIONS)


def run_scanner(root_dir: str, exclude_dirs, silent_mode: bool):
    if os.path.isdir(root_dir):
        for directory, dirs, files in os.walk(root_dir, topdown=True):
            [
                dirs.remove(excluded_dir)
                for excluded_dir in list(dirs)
                if os.path.join(directory, excluded_dir) in exclude_dirs
            ]

            for filename in files:
                if acceptable_filename(filename):
                    full_path = os.path.join(directory, filename)
                    rel_path = os.path.relpath(full_path, root_dir)
                    try:
                        with open(full_path, "rb") as file:
                            test_file(file, rel_path, silent_mode)
                    except FileNotFoundError as fnf_error:
                        if not silent_mode:
                            print(fnf_error)
    elif os.path.isfile(root_dir):
        if acceptable_filename(root_dir):
            with open(root_dir, "rb") as file:
                if any(root_dir.endswith(ext) for ext in ZIP_EXTENSIONS):
                    zip_file(file, "", silent_mode)

                elif any(root_dir.endswith(ext) for ext in TAR_EXTENSIONS):
                    tar_file(file, "", silent_mode)


def print_usage():
    print(
        "Usage: "
        + sys.argv[0]
        + " <root_folder> [-quiet] [-exclude <folder1> <folder2> ...]"
    )
    print("or: " + sys.argv[0] + "<archive_file> [-quiet]")
    exit()


def parse_command_line():
    if len(sys.argv) < 2:
        print_usage()

    root_dir = sys.argv[1]
    exclude_folders = []

    silent = len(sys.argv) > 2 and sys.argv[2] == "-quiet"
    exclude_start = 3 if silent else 2
    if len(sys.argv) > exclude_start:
        if not sys.argv[exclude_start] == "-exclude":
            print_usage()
        exclude_folders = sys.argv[exclude_start + 1 :]

    return root_dir, exclude_folders, silent


if __name__ == "__main__":
    root_dir, exclude_dirs, silent_mode = parse_command_line()

    for dir_to_check in exclude_dirs:
        if not os.path.isdir(dir_to_check):
            print(dir_to_check + " is not a directory")
            print_usage()
    if not os.path.isdir(root_dir) and not (
        os.path.isfile(root_dir) and acceptable_filename(root_dir)
    ):
        print(root_dir + " is not a directory or an archive")
        print_usage()

    print("Scanning " + root_dir)
    if exclude_dirs:
        print("Excluded: " + ", ".join(exclude_dirs))

    run_scanner(root_dir, set(exclude_dirs), silent_mode)
