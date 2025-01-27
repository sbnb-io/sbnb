# This script automates Sbnb Linux bootable USB creation process on Windows.
# It downloads, decompresses, and installs the sbnb.raw file onto a selected disk.
# It also allows the user to provide a Tailscale key and a custom script to be executed during Sbnb Linux boot.
# More info at https://github.com/sbnb-io/sbnb

param (
    [string]$SbnbRawPath
)

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " Welcome to Sbnb Linux Bootable USB Creation " -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan

# Step 1: Find latest release and download sbnb.raw.zip if not provided
if (-not $SbnbRawPath) {
    $repoUrl = "https://api.github.com/repos/sbnb-io/sbnb/releases/latest"
    $releaseInfo = Invoke-RestMethod -Uri $repoUrl
    $latestRelease = $releaseInfo.tag_name
    $downloadUrl = $releaseInfo.assets | Where-Object { $_.name -eq "sbnb.raw.zip" } | Select-Object -ExpandProperty browser_download_url

    Write-Host "Sbnb Linux latest release: $latestRelease" -ForegroundColor Green
    Write-Host "Download URL: $downloadUrl" -ForegroundColor Green

    # Get the size of the download
    $webRequest = Invoke-WebRequest -Uri $downloadUrl -Method Head
    $fileSize = [math]::Round($webRequest.Headers['Content-Length'] / 1MB, 2)

    # Ask user if they agree to continue
    $confirmation = Read-Host "The file size is $fileSize MB. Do you want to continue with the download? (y/n)"
    if ($confirmation -ne "y") {
        Write-Host "Download cancelled." -ForegroundColor Red
        exit
    }

    Write-Host "Downloading sbnb.raw.zip..." -ForegroundColor Yellow
    $wc = New-Object net.webclient
    $wc.DownloadFile($downloadUrl, "sbnb.raw.zip")

    # Step 2: Decompress sbnb.raw.zip to a temporary directory
    $tempDir = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), [System.IO.Path]::GetRandomFileName())
    [System.IO.Directory]::CreateDirectory($tempDir)

    Write-Host "Decompressing sbnb.raw.zip to $tempDir..." -ForegroundColor Yellow
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory("sbnb.raw.zip", $tempDir)

    $SbnbRawPath = "$tempDir\sbnb.raw"
} else {
    Write-Host "Using provided sbnb.raw file: $SbnbRawPath" -ForegroundColor Green
}

# Step 3: Enumerate all disks and ask user to input disk number
$disks = Get-WmiObject -Query "SELECT * FROM Win32_DiskDrive" | Sort-Object -Property Model
Write-Host "Available Disks:" -ForegroundColor Cyan
for ($i = 0; $i -lt $disks.Count; $i++) {
    $diskInfo = "${i}: $($disks[$i].DeviceID) - $($disks[$i].Model) - $($disks[$i].Name)"
    Write-Host $diskInfo -ForegroundColor Green
}

$selectedDiskNumber = Read-Host "Enter the number of the disk to flash into"
$selectedDrive = $disks[$selectedDiskNumber].Index

# Step 4: Double confirm user agrees with selection
Write-Host "WARNING: You have selected disk $($disks[$selectedDiskNumber].Model) (Index: $selectedDrive)." -ForegroundColor Red -BackgroundColor Yellow
Write-Host "ALL DATA ON THIS DISK WILL BE DESTROYED!" -ForegroundColor Red -BackgroundColor Yellow
$confirmation = Read-Host "Are you absolutely sure you want to proceed? (y/n)"
if ($confirmation -ne "y") {
    Write-Host "Operation cancelled." -ForegroundColor Red
    exit
}

# Step 5: Write sbnb.raw to the selected disk
Write-Host "Writing sbnb.raw to disk $($disks[$selectedDiskNumber].Model) (Index: $selectedDrive)..." -ForegroundColor Yellow
$disk = Get-Disk -Number $selectedDrive
$disk | Clear-Disk -RemoveData -Confirm:$false
$disk | Set-Disk -IsOffline $false
$disk | Set-Disk -IsReadOnly $false

$rawFile = [System.IO.File]::OpenRead($SbnbRawPath)
$diskStream = [System.IO.FileStream]::new("\\.\PhysicalDrive$selectedDrive", [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write)
$rawFile.CopyTo($diskStream)
$rawFile.Close()
$diskStream.Close()

# Reread partitions from disk after writing sbnb.raw
Write-Host "Rereading partitions from disk $($disks[$selectedDiskNumber].Model) (Index: $selectedDrive)..." -ForegroundColor Yellow
$disk | Update-Disk

# Step 6: Change first partition GPT type to normal data GUID
Write-Host "Changing first partition GPT type to normal data GUID..." -ForegroundColor Yellow
Set-Partition -DiskNumber $selectedDrive -PartitionNumber 1 -GptType "{EBD0A0A2-B9E5-4433-87C0-68B6B72699C7}"

$partition = Get-Partition -DiskNumber $selectedDrive | Select-Object -First 1

# Assign a drive letter to the first partition
Write-Host "Assigning drive letter to the first partition..." -ForegroundColor Yellow
$driveLetter = (66..90 | ForEach-Object {[char]$_} | Where-Object { -not (Get-Volume -FileSystemLabel $_ -ErrorAction SilentlyContinue) })[0]

if (-not $partition.DriveLetter) {
    $accessPath = "$driveLetter`:\"
    Add-PartitionAccessPath -DiskNumber $selectedDrive -PartitionNumber $partition.PartitionNumber -AccessPath $accessPath
    Write-Host "Assigned drive letter $driveLetter to the first partition." -ForegroundColor Green
} else {
    Write-Host "Drive letter $($partition.DriveLetter) is already assigned to the first partition." -ForegroundColor Green
}

# Step 7: Place sbnb-tskey.txt and sbnb-cmds.sh on the first partition
$partition = Get-Partition -DiskNumber $selectedDrive | Select-Object -First 1
$espDriveLetter = $partition.DriveLetter
if (-not [string]::IsNullOrEmpty($espDriveLetter)) {
    $espPath = "$($espDriveLetter):\"
} else {
    Write-Host "Error: Unable to determine the ESP drive letter." -ForegroundColor Red
    exit
}

# Step 7.1: Ask user to provide Tailscale key and place it on the ESP partition
$tailscaleKey = Read-Host "Please provide your Tailscale key (press Enter to skip)"
if (-not [string]::IsNullOrEmpty($tailscaleKey)) {
    $tailscaleKeyPath = "$($espDriveLetter):\sbnb-tskey.txt"
    Write-Host "Tailscale key saving to $tailscaleKeyPath" -ForegroundColor Yellow
    [System.IO.File]::WriteAllText($tailscaleKeyPath, $tailscaleKey)
    Write-Host "Tailscale key saved to $tailscaleKeyPath" -ForegroundColor Green
} else {
    Write-Host "No Tailscale key provided. Skipping this step." -ForegroundColor Yellow
}

# Step 7.2: Ask user to provide the path to a script file and place it on the ESP partition
$scriptFilePath = Read-Host "Please provide the path to your script file (this script will be saved to sbnb-cmds.sh and executed during Sbnb Linux boot) (press Enter to skip)"
$scriptFilePath = $scriptFilePath.Trim('"')
if (-not [string]::IsNullOrEmpty($scriptFilePath) -and (Test-Path $scriptFilePath)) {
    $scriptContent = Get-Content -Path $scriptFilePath -Raw
    # Convert to Unix format by replacing Windows line endings with Unix line endings
    $scriptContent = $scriptContent -replace "`r`n", "`n"

    $scriptPath = "$($espDriveLetter):\sbnb-cmds.sh"
    [System.IO.File]::WriteAllText($scriptPath, $scriptContent)
    Write-Host "Script saved to $scriptPath" -ForegroundColor Green
} else {
    Write-Host "No valid script file provided. Skipping this step." -ForegroundColor Yellow
}
# Step 7.3: Remove the drive letter from the first partition
Write-Host "Removing drive letter from the first partition..." -ForegroundColor Yellow
Remove-PartitionAccessPath -DiskNumber $selectedDrive -PartitionNumber $partition.PartitionNumber -AccessPath "$($partition.DriveLetter):\"
Write-Host "Drive letter removed from the first partition." -ForegroundColor Green

# Step 8: Change GPT type to EFI
Write-Host "Changing GPT partition type to EFI..." -ForegroundColor Yellow
Set-Partition -DiskNumber $selectedDrive -PartitionNumber $partition.PartitionNumber -GptType "{C12A7328-F81F-11D2-BA4B-00A0C93EC93B}"

Write-Host "=========================================" -ForegroundColor Cyan
Write-Host " Operation completed successfully. " -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
