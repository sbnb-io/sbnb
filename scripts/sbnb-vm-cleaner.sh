#!/bin/sh

# This script cleans up unused image folders in the /mnt/sbnb-data/images/ directory.
# It lists all image folders, checks if they are associated with any running qemu processes,
# and removes those that are not in use.

# Get list of all image folders
all_folders=$(ls -d /mnt/sbnb-data/images/sbnb-vm-*)

# List available folders
echo "Available folders:"
echo "$all_folders" | tr ' ' '\n'

# Loop through all folders and remove those not in running images
for folder in $all_folders; do
    folder_basename=$(basename "$folder")
    
    # Check if the folder_basename is in the list of running processes
    if ! ps -A | grep -v grep | grep -q "$folder_basename"; then
        echo "Removing folder: $folder"
        rm -rf "$folder"
    else
        echo "Image in use, not removing: $folder"
    fi
done
