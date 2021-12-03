#!/usr/bin/env python3

'''
This file is created by Descript to document and augment
the FFmpeg building process, for use in Descript's environment.

(1) Call build-ffmpeg with the build command
(2) Copy or generate dSYM symbol files to the workspace folder
(3) Copy executables from the workspace folder and all built dependencies to platform output folder
(4) Fix dyld ids and loader paths for all built libraries
(5) Zip up the build artifacts
'''

import glob
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import zipfile

#
#   Constants
#
cwd = os.path.dirname(os.path.realpath(__file__))
base_dir = pathlib.Path(cwd).parent.absolute()
packages_dir = os.path.join(base_dir, 'packages')
workspace_dir = os.path.join(base_dir, 'workspace')
workspace_bin_dir = os.path.join(workspace_dir, 'bin')
workspace_lib_dir = os.path.join(workspace_dir, 'lib')
deployment_target = '11.0' if platform.machine() == 'arm64' else '10.11' 

#
#   Keep track of which libraries are copied, skipped, or missing
#
skipped_libs = set()
copied_libs = set()
missing_libs = set()

#
#
#
def buildFFmpeg(script_dir, log_file):
    """
    builds FFmpeg and logs output to `log_file`
    """
    # set environment variables
    env = os.environ
    env['SKIPINSTALL'] = 'yes'  # append 'SKIPINSTALL=yes' to skip prompt for installing FFmpeg to /usr/local/bin/etc
    env['VERBOSE'] = 'yes'
    env['MACOSX_DEPLOYMENT_TARGET'] = deployment_target
    
    # call main build script
    build_ffmpeg_path = os.path.join(script_dir, 'build-ffmpeg')
    args = [
        build_ffmpeg_path,
        '-b',                       # build
        '--full-shared',            # custom Descript shim to build shared libraries instead of static
        '--enable-gpl-and-free']    # custom Descript shim to build GPL but not non-free (libpostproc is needed by Beamcoder and requires GPL)
    log_file.write(' '.join(args) + '\n\n')    
    subprocess.call(args, env=env, stdout=log_file)

#
#
#
def copyOrGenerateSymbolFile(file, dest, log_file):
    """
    Copies a single symbol file to the workspace destination
    skips symlinks to avoid duplication
    Copies entire `dSYM` packages for `dylib` files already within `.dSYM` packages
    """
    fileref = pathlib.Path(file)
    if not fileref.is_symlink():
        symbolFileName = fileref.name + '.dSYM'
        destPath = os.path.join(dest, symbolFileName)
        
        # See if there's a matching pre-existing symbol file.
        # If so, copy it, replacing the destination
        # example:
        #   ./packages/libtheora-1.1.1/lib/.libs/libtheoraenc.1.dylib.dSYM/Contents/Resources/DWARF/libtheoraenc.1.dylib
        try:
            allParts = fileref.parts
            symbolDirIndex = allParts.index(symbolFileName) # throws ValueError if not in allParts
            symbolDirParts = allParts[:symbolDirIndex + 1]
            symbolDir = os.path.join(*symbolDirParts)
            if os.path.exists(destPath):
                shutil.rmtree(destPath)
            shutil.copytree(symbolDir, destPath)

        # Otherwise, generate a symbol file and place it at the destination
        # example:
        #   ./packages/libtheora-1.1.1/lib/.libs/libtheora.dylib
        except ValueError as e:
            args = ['/usr/bin/dsymutil', str(fileref), '-o', destPath]
            log_file.write(' '.join(args) + '\n')
            subprocess.call(args, stdout=log_file)

#
#
#
def copyOrGenerateSymbolFiles(source, dest, log_file):
    """
    Recursively copies symbol files to the workspace destination
    skips symlinks to avoid duplication
    Copies entire `dSYM` packages for `dylib` files already within `.dSYM` packages
    """
    for fileref in pathlib.Path(source + '/').glob('**/*.dylib'):
      copyOrGenerateSymbolFile(str(fileref), dest, log_file)
    for fileref in pathlib.Path(source + '/').glob('**/*.so*'):
      copyOrGenerateSymbolFile(str(fileref), dest, log_file)

#
#
#
def readDeploymentTarget(src_file) -> str:
    """
    Reads the deployment target of a binary
    :return: something like `'10.11'` or an empty string
    """
    args = ['/usr/bin/otool', '-l', src_file]
    otool_proc = subprocess.Popen(args, stdout=subprocess.PIPE)
    inLoaderCommand = False
    for line in otool_proc.stdout:
        ln = line.decode('utf-8').strip()
        if inLoaderCommand:
            if ln.startswith('minos') or ln.startswith('version'):
                return ln.split(' ')[1]
            if ln.startswith('sdk'):
                continue
        elif 'LC_VERSION_MIN_MACOSX' in ln or 'LC_BUILD_VERSION' in ln:
            inLoaderCommand = True

    return ''
           

#
#
#
def copyLibraryAndSymbolPackage(src_file, dest_folder, overwrite):
    """
    Copies a library and its corresponding `.dSYM` bundle
    (if present)
    """
    this_deployment_target = readDeploymentTarget(src_file)
    assert this_deployment_target == deployment_target, '{0} wrong deployment target {1}'.format(src_file, this_deployment_target)

    dest_file = os.path.join(dest_folder, os.path.basename(src_file))
    
    # copy file
    if overwrite and os.path.exists(dest_file):
        os.remove(dest_file)
    shutil.copy2(src_file, dest_file, follow_symlinks=False)

    # copy symbol file
    src_symbol_package = src_file + '.dSYM'
    if os.path.exists(src_symbol_package):
        dest_symbol_package = os.path.join(dest_folder, os.path.basename(src_symbol_package))
        if overwrite and os.path.exists(dest_symbol_package):
          shutil.rmtree(dest_symbol_package)
        if not os.path.exists(dest_symbol_package):
          shutil.copytree(src_symbol_package, dest_symbol_package)

#
#
#
def getFilenameWithoutVersion(file_name) -> str:
    """
    :return: `'libSDL2'` for something like `'libSDL2-2.0.0.dylib'`
    """
    result = file_name.split('.')[0]
    # libSDL2 weirdly has hypthen after then name (i.e., libSDL2-2.0.0.dylib)
    if 'libSDL2' in result:
        result = 'libSDL2'
    return result

#
#
#
def copyLibraryAndDependencies(src_file, dest_folder, log_file):
    """
    Recursive function to copy a library and its (non-system) dependencies
    also fixes loader paths for each library to be `@loader_path`
    """

    dest_file = os.path.join(dest_folder, os.path.basename(src_file))

    # copy file
    copyLibraryAndSymbolPackage(src_file, dest_folder, True)
    copied_libs.add(src_file)
    copied_libs.add(dest_file)

    # identifier for _this_ library
    this_id = ''

    # recursively copy dependencies
    args = ['/usr/bin/otool', '-L', src_file]
    otool_proc = subprocess.Popen(args, stdout=subprocess.PIPE)
    loader_paths_to_rewrite = []
    for line in otool_proc.stdout:
        ln = line.decode('utf-8').strip()
        match = re.match('[^\s:]+', ln)
        if not match:
            continue
        src_dependency_file = match[0]

        # fix incorrect usage of @rpath
        if src_dependency_file.startswith('@rpath/'):
            fixed_path = os.path.join(workspace_lib_dir, src_dependency_file[7:])
            loader_paths_to_rewrite.append({'old_path': src_dependency_file, 'new_path': fixed_path})
            src_dependency_file = fixed_path

        if src_dependency_file.startswith('/usr/local'):
            # the build grabbed libraries installed on this machine
            # which might not be available on other machines
            missing_libs.add(src_dependency_file)
        elif src_dependency_file.startswith(workspace_dir):
            dependency_name = os.path.basename(src_dependency_file)
            if not len(this_id):
                # first dependency is the identifier for this library
                this_id = dependency_name
            dest_dependency_path = os.path.join(dest_folder, dependency_name)
            if not src_dependency_file in copied_libs:
                if src_dependency_file != dest_dependency_path:
                    # Copy each version variant file (often symlinks)
                    dependency_name_without_version = getFilenameWithoutVersion(src_dependency_file)
                    unversioned_dependency_base_name = os.path.join(os.path.dirname(src_dependency_file), dependency_name_without_version)
                    for variant_src_file in glob.glob(unversioned_dependency_base_name + r'*.dylib'):
                        copyLibraryAndSymbolPackage(variant_src_file, dest_folder, False)
                        variant_dest_file = os.path.join(dest_folder, os.path.basename(variant_src_file))
                        copied_libs.add(variant_src_file)
                        copied_libs.add(variant_dest_file)

                    # RECURSIVELY copy dependencies
                    if (os.path.exists(unversioned_dependency_base_name + '.dylib')):
                        copyLibraryAndDependencies(unversioned_dependency_base_name + '.dylib', dest_folder, log_file)
            
            loader_paths_to_rewrite.append({'old_path': src_dependency_file, 'new_path': dest_dependency_path})
        else:
            skipped_libs.add(src_dependency_file)

    # find the non-sym-linked version of this library
    actual_binary_path = os.path.realpath(dest_file)

    # correct the loader path for this library
    if len(this_id):
        args = ['/usr/bin/install_name_tool', '-id', '@loader_path/' + this_id, actual_binary_path]
        log_file.write(' '.join(args) + '\n')
        subprocess.check_output(args)
    
    # correct the loader paths for all dependencies
    if len(loader_paths_to_rewrite) > 0:
        for loader_path in loader_paths_to_rewrite:
            args = ['/usr/bin/install_name_tool', '-change', loader_path['old_path'], '@loader_path/' + os.path.basename(loader_path['new_path']), actual_binary_path]
            log_file.write(' '.join(args) + '\n')
            subprocess.check_output(args)

#
#
#
def readVersion() -> str:
    """
    Reads the version string from ../build-ffmpeg
    :return: something like `'1.31rc1'`
    """
    result = ''
    with open(os.path.join(base_dir, 'build-ffmpeg')) as f:
        lines = f.readlines()
        for line in lines:
            if line.startswith('SCRIPT_VERSION='):
                result = line[15:].strip()
    return result

#
#
#
def getPlatformMachineVersion() -> str:
    """
    :return: a string like `'darwin-x86_64.1.31rc2'`
    """
    return sys.platform + '-' + platform.machine() + '.' + readVersion()


#
#
#
def generateChecksum(output_folder):
    """
    Calculates checksums for every file in `output_folder`
    and puts it in a `SHAMSUM256.txt` file
    """
    checksums = set()

    # calculate checksums for all files
    for (dirpath, dirnames, filenames) in os.walk(output_folder):
        for file in filenames:
            args = ['shasum', '-a', '256', os.path.join(dirpath, file)]
            output = subprocess.check_output(args)
            checksum = output.decode('utf-8').strip()

            # replace absolute path to just filename
            # From: '0a88d3f97f356c6a42449fd548f9b586f565899144849019014e36c7683b745e  /Users/cvanwink/Source/git/electron/src/out/Testing/dist.zip'
            # To:   '0a88d3f97f356c6a42449fd548f9b586f565899144849019014e36c7683b745e  *electron-v13.1.6-darwin-x64.zip'
            checksum = checksum.replace(os.path.join(dirpath, ''), '*')
            checksums.add(checksum)
        break
    
    # Write Checksums to file
    checksum_file_path = os.path.join(output_folder, 'SHAMSUM256.txt')
    checksum_file = open(checksum_file_path, 'w')
    for checksum in checksums:
        checksum_file.write(f'{checksum}\n')
    checksum_file.close()

#
#
#
def main():
    output_dir = os.path.join(cwd, 'mac')
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    temp_dir = os.path.join(output_dir, platform.machine())
    os.makedirs(temp_dir)
    symbol_temp_dir = os.path.join(output_dir, platform.machine() + '-symbols')

    executables = ['ffmpeg', 'ffprobe']
    base_artifact_name = '-'.join(executables) + '-shared-' + getPlatformMachineVersion()

    # create a log file for the build-ffmpeg command for build archival purposes
    log_file_name = base_artifact_name + '-log.txt'
    log_file_path = os.path.join(output_dir, log_file_name)
    build_ffmpeg_log_file = open(log_file_path, 'w')

    build_ffmpeg_log_file.write('Begin build-ffmpeg-descript.py\n')
    build_ffmpeg_log_file.write('=======================\n')

    # Run the script
    buildFFmpeg(base_dir, build_ffmpeg_log_file)
    
    # Generate dSYM files for each built library
    build_ffmpeg_log_file.write('\nGenerating Symbols\n')
    build_ffmpeg_log_file.write('=======================\n')
    copyOrGenerateSymbolFiles(packages_dir, symbol_temp_dir, build_ffmpeg_log_file)
    symbol_file_name = base_artifact_name + '-symbols'
    shutil.make_archive(os.path.join(output_dir, symbol_file_name), 'zip', symbol_temp_dir)
    shutil.rmtree(symbol_temp_dir)

    # Generate dSYM files for each executable
    # and copy their dependencies
    for executable in executables:
        build_ffmpeg_log_file.write('\nCopying & Linking ' + executable + '\n')
        build_ffmpeg_log_file.write('=======================\n')
        executable_path = os.path.join(workspace_bin_dir, executable)
        copyOrGenerateSymbolFile(executable_path, workspace_bin_dir, build_ffmpeg_log_file)
        copyLibraryAndDependencies(executable_path, temp_dir, build_ffmpeg_log_file)

        # check that the copied file is runnable
        build_ffmpeg_log_file.write('\nChecking ' + executable + '\n')
        build_ffmpeg_log_file.write('=======================\n')
        args = [os.path.join(temp_dir, executable), '-version']
        build_ffmpeg_log_file.write(' '.join(args) + '\n')
        output = subprocess.check_output(args)
        build_ffmpeg_log_file.write(output.decode('utf-8'))

    # Copy Includes
    shutil.copytree(
      os.path.join(workspace_dir, 'include'),
      os.path.join(temp_dir, 'include'))

    build_ffmpeg_log_file.write('\nLibrary Info\n')
    build_ffmpeg_log_file.write('=======================\n')

    for lib in sorted(missing_libs):
      build_ffmpeg_log_file.write('[WARNING] missing ' + lib + '\n')

    for lib in sorted(skipped_libs):
      build_ffmpeg_log_file.write('[NOTE] skipped ' + lib + '\n')

    for lib in sorted(copied_libs):
      build_ffmpeg_log_file.write('Copied ' + lib + '\n')

    build_ffmpeg_log_file.write('\nArchiving third-party source\n')
    build_ffmpeg_log_file.write('=======================\n')

    # bundle up the third-party source
    # grab each .tar.* from the packages folder
    packages_zip_name = base_artifact_name + '-packages.zip'
    with zipfile.ZipFile(os.path.join(output_dir, packages_zip_name), 'w', zipfile.ZIP_DEFLATED) as myzip:
        archives = pathlib.Path(packages_dir + '/').glob('*.tar.*')
        for archive in sorted(archives, key=lambda s: str(s).lower()):
            build_ffmpeg_log_file.write(os.path.join('packages', archive.name) + '\n')
            myzip.write(str(archive.absolute()), archive.name)

    build_ffmpeg_log_file.write('\nArchiving libraries\n')
    build_ffmpeg_log_file.write('=======================\n')

    # bundle up the build artifacts
    os.chdir(temp_dir)
    shared_zip_name = base_artifact_name + '.zip'
    dest_file = os.path.join(output_dir, shared_zip_name)
    args = ['/usr/bin/zip', '--symlinks', '-r', os.path.join('..', shared_zip_name), '.']
    build_ffmpeg_log_file.write(' '.join(args) + '\n')
    subprocess.check_output(args)

    shutil.rmtree(temp_dir)
    
    build_ffmpeg_log_file.write('\nEnd of build-ffmpeg-descript.py\n')
    build_ffmpeg_log_file.write('=======================\n')
    build_ffmpeg_log_file.close()

    # zip up log file
    with zipfile.ZipFile(os.path.splitext(log_file_path)[0] + '.zip', 'w', zipfile.ZIP_DEFLATED) as myzip:
        myzip.write(log_file_path, os.path.basename(log_file_path))
    os.remove(log_file_path)

    generateChecksum(output_dir)

#
#   entry
#
if __name__ == '__main__':
    main()
