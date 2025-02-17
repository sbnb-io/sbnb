#!/bin/bash

# This script automates Sbnb Linux bootable USB creation process on Linux.
# It downloads, decompresses, and installs the sbnb.raw file onto a selected disk.
# It also allows the user to provide a Tailscale key and a custom script to be executed during Sbnb Linux boot.
# More info at https://github.com/sbnb-io/sbnb

# Exit immediately if a command exits with a non-zero status
set -e

# Define color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=========================================${NC}"
echo -e "${GREEN} Welcome to Sbnb Linux Bootable USB Creation ${NC}"
echo -e "${BLUE}=========================================${NC}"

# Step 1: Find latest release and download sbnb.raw.zip if not provided
if [ -z "$1" ]; then
    repoUrl="https://api.github.com/repos/sbnb-io/sbnb/releases/latest"
    releaseInfo=$(curl -s $repoUrl)
    latestRelease=$(echo $releaseInfo | grep -oE '"tag_name": "[^"]+"' | cut -d '"' -f 4)
    downloadUrl=$(echo $releaseInfo | grep -oE '"browser_download_url": "[^"]+"' | cut -d '"' -f 4 | grep "sbnb.raw.zip")

    echo -e "${YELLOW}Sbnb Linux latest release: $latestRelease${NC}"
    echo -e "${YELLOW}Download URL: $downloadUrl${NC}"

    # Get the size of the download
    redirectUrl=$(curl -sI $downloadUrl | grep -i "location:" | awk '{print $2}' | tr -d '\r')
    fileSize=$(curl -sI $redirectUrl | grep -i Content-Length | awk '{print $2}' | tr -d '\r')
    fileSizeMB=$(echo "scale=2; $fileSize / 1024 / 1024" | bc)

    # Ask user if they agree to continue
    read -p "The file size is $fileSizeMB MB. Do you want to continue with the download? (y/n) " confirmation
    if [ "$confirmation" != "y" ]; then
        echo -e "${RED}Download cancelled.${NC}"
        exit
    fi

    echo -e "${YELLOW}Downloading sbnb.raw.zip...${NC}"
    curl -L -o sbnb.raw.zip $downloadUrl

    # Step 2: Decompress sbnb.raw.zip to a temporary directory
    tempDir=$(mktemp -d)
    echo -e "${YELLOW}Decompressing sbnb.raw.zip to $tempDir...${NC}"
    unzip sbnb.raw.zip -d $tempDir

    SbnbRawPath="$tempDir/sbnb.raw"
else
    SbnbRawPath="$1"
    echo -e "${YELLOW}Using provided sbnb.raw file: $SbnbRawPath${NC}"
fi

# Step 3: Enumerate all disks and ask user to input disk number
echo -e "${YELLOW}Available Disks:${NC}"
lsblk -o NAME,SIZE,MODEL | grep -E '^sd|^nvme' | nl

read -p "Enter the index number of the disk to flash into: " selectedDiskIndex
selectedDiskName=$(lsblk -o NAME | grep -E '^sd|^nvme' | sed -n "${selectedDiskIndex}p")
selectedDrive="/dev/$selectedDiskName"

# Step 4: Double confirm user agrees with selection
echo -e "${RED}WARNING: You have selected disk $selectedDrive.${NC}"
echo -e "${RED}ALL DATA ON THIS DISK WILL BE DESTROYED!${NC}"
read -p "Are you absolutely sure you want to proceed? (y/n) " confirmation
if [ "$confirmation" != "y" ]; then
    echo -e "${RED}Operation cancelled.${NC}"
    exit
fi

# Unmount the selected disk if it is mounted
if mount | grep "$selectedDrive" > /dev/null; then
    echo -e "${YELLOW}Unmounting disk $selectedDrive...${NC}"
    mountedPartitions=$(mount | grep "^$selectedDrive" | awk '{print $1}')
    if [ -n "$mountedPartitions" ]; then
        for partition in $mountedPartitions; do
            echo -e "${YELLOW}Unmounting partition $partition...${NC}"
            sudo umount "$partition"
        done
    else
        echo -e "${YELLOW}No partitions of disk $selectedDrive are mounted. Skipping unmount.${NC}"
    fi
fi

# Step 5: Write sbnb.raw to the selected disk
echo -e "${YELLOW}Writing sbnb.raw to disk $selectedDrive...${NC}"
sudo dd if=$SbnbRawPath of=$selectedDrive bs=1M

# Step 6: Mount the newly written disk
echo -e "${YELLOW}Mounting disk $selectedDrive...${NC}"
tempMountDir=$(mktemp -d -t sbnb-esp-XXXXXX)
sudo mount ${selectedDrive}1 $tempMountDir

# Step 7: Ensure the ESP partition is mounted to the temporary directory
espPath="$tempMountDir"
if mount | grep "on $espPath" > /dev/null; then
    echo -e "${YELLOW}ESP partition is mounted at $espPath${NC}"
else
    echo -e "${RED}Error: ESP partition is not mounted at $espPath.${NC}"
    exit
fi

# Step 7.1: Ask user to provide Tailscale key and place it on the ESP partition
read -p "Please provide your Tailscale key (press Enter to skip): " tailscaleKey
if [ -n "$tailscaleKey" ]; then
    tailscaleKeyPath="$espPath/sbnb-tskey.txt"
    echo -e "${YELLOW}Tailscale key saving to $tailscaleKeyPath${NC}"
    echo "$tailscaleKey" > $tailscaleKeyPath
    echo -e "${YELLOW}Tailscale key saved to $tailscaleKeyPath${NC}"
else
    echo -e "${YELLOW}No Tailscale key provided. Skipping this step.${NC}"
fi

# Step 7.2: Ask user to provide the path to a script file and place it on the ESP partition
read -p "Please provide the path to your script file (this script will be saved to sbnb-cmds.sh and executed during Sbnb Linux boot) (press Enter to skip): " scriptFilePath
if [ -n "$scriptFilePath" ] && [ -f "$scriptFilePath" ]; then
    scriptContent=$(cat "$scriptFilePath")
    scriptPath="$espPath/sbnb-cmds.sh"
    echo "$scriptContent" > $scriptPath
    echo -e "${YELLOW}Script saved to $scriptPath${NC}"
else
    echo -e "${YELLOW}No valid script file provided. Skipping this step.${NC}"
fi

# Step 8: Unmount the disk
echo -e "${YELLOW}Unmounting disk $selectedDrive...${NC}"
sudo umount $espPath

# Step 9: Cleanup temporary directory
echo -e "${YELLOW}Cleaning up temporary files...${NC}"
rm -rf $tempDir
echo -e "${YELLOW}Temporary files cleaned up.${NC}"

echo -e "${BLUE}=========================================${NC}"
echo -e "${GREEN} Operation completed successfully. ${NC}"
echo -e "${BLUE}=========================================${NC}"
