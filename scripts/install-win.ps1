# This script automates Sbnb Linux bootable USB creation process on Windows.
# It downloads and installs the sbnb.efi file onto a selected disk.
# It also allows the user to provide a Tailscale key and a custom script to be executed during Sbnb Linux boot.
#
# More info at https://github.com/sbnb-io/sbnb

param (
    [string]$SbnbEfiPath
)

Write-Output "Welcome to Sbnb Linux bootable USB creation"

# Step 1: Find latest release and download sbnb.efi.zip if not provided
if (-not $SbnbEfiPath) {
    $repoUrl = "https://api.github.com/repos/sbnb-io/sbnb/releases/latest"
    $releaseInfo = Invoke-RestMethod -Uri $repoUrl
    $latestRelease = $releaseInfo.tag_name
    $downloadUrl = $releaseInfo.assets | Where-Object { $_.name -eq "sbnb.efi.zip" } | Select-Object -ExpandProperty browser_download_url

    Write-Output "Sbnb Linux latest release: $latestRelease"
    Write-Output "Download URL: $downloadUrl"

    # Get the size of the download
    $webRequest = Invoke-WebRequest -Uri $downloadUrl -Method Head
    $fileSize = [math]::Round($webRequest.Headers['Content-Length'] / 1MB, 2)

    # Ask user if they agree to continue
    $confirmation = Read-Host "The file size is $fileSize MB. Do you want to continue with the download? (y/n)"
    if ($confirmation -ne "y") {
        Write-Output "Download cancelled."
        exit
    }

    Write-Output "Downloading sbnb.efi.zip..."
    $wc = New-Object net.webclient
    $wc.DownloadFile($downloadUrl, "sbnb.efi.zip")

    # Step 2: Decompress sbnb.efi.zip to a temporary directory
    $tempDir = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), [System.IO.Path]::GetRandomFileName())
    [System.IO.Directory]::CreateDirectory($tempDir)

    Write-Output "Decompressing sbnb.efi.zip to $tempDir..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory("sbnb.efi.zip", $tempDir)

    $SbnbEfiPath = "$tempDir\sbnb.efi"
} else {
    Write-Output "Using provided sbnb.efi file: $SbnbEfiPath"
}

# Step 3: Enumerate all disks and ask user to input disk number
$disks = Get-WmiObject -Query "SELECT * FROM Win32_DiskDrive" | Sort-Object -Property Model
for ($i = 0; $i -lt $disks.Count; $i++) {
    $diskInfo = "${i}: $($disks[$i].DeviceID) - $($disks[$i].Model) - $($disks[$i].Name)"
    Write-Host $diskInfo
}

$selectedDiskNumber = Read-Host "Enter the number of the disk to flash into"
$selectedDrive = $disks[$selectedDiskNumber].Index

# Step 4: Double confirm user agrees with selection
$confirmation = Read-Host "You have selected disk $($disks[$selectedDiskNumber].Model) (Index: $selectedDrive). All data on this disk will be destroyed. Are you sure you want to proceed? (y/n)"
if ($confirmation -ne "y") {
    Write-Output "Operation cancelled."
    exit
}

# Step 5: Remove all existing partitions and create ESP partition
Write-Output "Removing all existing partitions on disk $($disks[$selectedDiskNumber].Model) (Index: $selectedDrive)..."
Get-Disk -Number $selectedDrive | Clear-Disk -RemoveData -Confirm:$false

# Get the size of the selected disk
$disk = Get-Disk -Number $selectedDrive
$diskSize = $disk.Size

# Determine the partition size
$maxSize = 16GB
$partitionSize = if ($diskSize -lt $maxSize) { $diskSize } else { $maxSize }

Write-Output "Creating ESP partition on disk $($disks[$selectedDiskNumber].Model) (Index: $selectedDrive) with a size of $partitionSize bytes..."
$partition = New-Partition -DiskNumber $disk.Number -Size $partitionSize -AssignDriveLetter
Start-Sleep -Seconds 5
Format-Volume -FileSystem FAT32 -NewFileSystemLabel "sbnb" -DriveLetter $partition.DriveLetter
Write-Output "Partition created and formatted with drive letter: $($partition.DriveLetter)"

# Step 6: Decompress and place sbnb.efi on the ESP partition
$espDriveLetter = $partition.DriveLetter
if (-not [string]::IsNullOrEmpty($espDriveLetter)) {
    $espPath = "$($espDriveLetter):\EFI\Boot"
    [System.IO.Directory]::CreateDirectory($espPath)
} else {
    Write-Output "Error: Unable to determine the ESP drive letter."
    exit
}

Write-Output "Copying sbnb.efi to $espPath\bootx64.efi..."
Copy-Item -Path $SbnbEfiPath -Destination "$espPath\bootx64.efi"

# Step 7: Ask user to provide Tailscale key and place it on the ESP partition
$tailscaleKey = Read-Host "Please provide your Tailscale key (press Enter to skip)"
if (-not [string]::IsNullOrEmpty($tailscaleKey)) {
    $tailscaleKeyPath = "$($espDriveLetter):\sbnb-tskey.txt"
    [System.IO.File]::WriteAllText($tailscaleKeyPath, $tailscaleKey)
    Write-Output "Tailscale key saved to $tailscaleKeyPath"
} else {
    Write-Output "No Tailscale key provided. Skipping this step."
}

# Step 7.1: Ask user to provide the path to a script file and place it on the ESP partition
$scriptFilePath = Read-Host "Please provide the path to your script file (this script will be saved to sbnb-cmds.sh and executed during Sbnb Linux boot) (press Enter to skip)"
$scriptFilePath = $scriptFilePath.Trim('"')
if (-not [string]::IsNullOrEmpty($scriptFilePath) -and (Test-Path $scriptFilePath)) {
    $scriptContent = Get-Content -Path $scriptFilePath -Raw
    # Convert to Unix format by replacing Windows line endings with Unix line endings
    $scriptContent = $scriptContent -replace "`r`n", "`n"
    
    $scriptPath = "$($espDriveLetter):\sbnb-cmds.sh"
    [System.IO.File]::WriteAllText($scriptPath, $scriptContent)
    Write-Output "Script saved to $scriptPath"
} else {
    Write-Output "No valid script file provided. Skipping this step."
}

# Step 8: Set GPT partition type
Write-Output "Setting GPT partition type to C12A7328-F81F-11D2-BA4B-00A0C93EC93B..."
Set-Partition -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -GptType "{C12A7328-F81F-11D2-BA4B-00A0C93EC93B}"

Write-Output "Operation completed successfully."
