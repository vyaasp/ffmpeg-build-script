# Build FFmpeg for Descript

## Author / Contact:
  - [Charles Van Winkle](https://github.com/cvanwinkle)
  - [Steve Rubin](https://github.com/srubin) 

## Instructions
- Run `build-ffmpeg-descript.py`


## Build Overview
The build script automates the following basic operations.
- Creates a log file to archive the compiler/linker and packaging steps
- Runs the modified `buildFFmpeg` shell script, outputs to log file
- Recursively generates or copies `.dSYM` symbol files for each dependency into a `.zip` file
  - Also fixes `dlyd` loader paths for each dependency
- Checks that each executable (i.e. `ffmpeg` & `ffprobe`) are runnable
- Copies `includes` header folder
- Checks for any linked dependencies which are linked to locations on the build machine and not present in the archive bundle
- Archives the upstream tar bundles for each `ffmpeg` component
  - This is important in case upstream FTP or source servers go offline in the future
- Generates checksum for each created artifact (build, symbols, packages, log)
 
## Development
Known issues:
- .

## Patches


