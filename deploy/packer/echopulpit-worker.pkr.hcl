packer {
  required_plugins {
    amazon = {
      version = ">= 1.2.8"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  type    = string
  default = "g4dn.xlarge"
}

variable "subnet_id" {
  type        = string
  default     = ""
  description = "Subnet to build in. Must have internet access (NAT or public + auto-assign IP) for apt/pip installs."
}

variable "whisper_model" {
  type    = string
  default = "medium"
}

variable "gguf_model_path" {
  type        = string
  default     = "../../models/mistral-7b-instruct-v0.2.Q5_K_S.gguf"
  description = "Local path to the GGUF model to bake into the AMI."
}

source "amazon-ebs" "echopulpit_worker" {
  region           = var.aws_region
  instance_type    = var.instance_type
  subnet_id        = var.subnet_id
  ssh_username     = "ubuntu"
  ami_name         = "echopulpit-worker-{{timestamp}}"
  ami_description  = "EchoPulpit worker: CUDA + Whisper + Mistral 7B baked in, single-job self-terminating"

  # AWS's Deep Learning Base AMI ships NVIDIA drivers + CUDA preinstalled,
  # which avoids doing that setup by hand here.
  source_ami_filter {
    filters = {
      name                = "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*"
      root-device-type    = "ebs"
      virtualization-type = "hvm"
    }
    owners      = ["amazon"]
    most_recent = true
  }

  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size            = 60
    volume_type             = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name    = "echopulpit-worker"
    Project = "echopulpit"
  }
}

build {
  sources = ["source.amazon-ebs.echopulpit_worker"]

  provisioner "shell" {
    inline = [
      "sudo mkdir -p /opt/echopulpit /models /data/output",
      "sudo chown -R ubuntu:ubuntu /opt/echopulpit /models /data/output",
    ]
  }

  # App code
  provisioner "file" {
    sources = [
      "../../sermon_pipeline.py",
      "../../sermon_heuristics.py",
      "../../prompts.py",
      "../../render_pdf.py",
      "../../storage.py",
      "../../requirements.txt",
      "../../config.yaml",
    ]
    destination = "/opt/echopulpit/"
  }

  provisioner "file" {
    source      = "../bootstrap.sh"
    destination = "/opt/echopulpit/bootstrap.sh"
  }

  provisioner "file" {
    source      = "../systemd/echopulpit-worker.service"
    destination = "/tmp/echopulpit-worker.service"
  }

  # Bake the LLM model in directly -- this is the single biggest thing that
  # makes ephemeral-instance economics work; without it, every job would
  # re-download several GB before doing any real work.
  provisioner "file" {
    source      = var.gguf_model_path
    destination = "/models/mistral-7b-instruct-v0.2.Q5_K_S.gguf"
  }

  provisioner "shell" {
    environment_vars = [
      "WHISPER_MODEL=${var.whisper_model}",
      "DEBIAN_FRONTEND=noninteractive",
    ]
    inline = [
      "set -eux",
      "sudo apt-get update",
      "sudo apt-get install -y --no-install-recommends software-properties-common curl unzip ca-certificates ffmpeg git build-essential cmake at",
      "sudo add-apt-repository -y ppa:deadsnakes/ppa",
      "sudo apt-get update",
      "sudo apt-get install -y --no-install-recommends python3.11 python3.11-venv python3.11-distutils",
      "curl -sS https://bootstrap.pypa.io/get-pip.py | sudo python3.11",

      # yt-dlp's JS runtime requirement
      "curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -",
      "sudo apt-get install -y --no-install-recommends nodejs",

      # AWS CLI (bootstrap.sh shells out to it for S3 sync + tag reads)
      "curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/awscliv2.zip",
      "cd /tmp && unzip -q awscliv2.zip && sudo ./aws/install",

      # llama-cpp-python built with CUDA support. GGML_CUDA is the current
      # flag name (older releases used LLAMA_CUBLAS) -- check this against
      # whatever llama-cpp-python version requirements.txt pins if the build
      # silently falls back to a CPU-only wheel.
      "CMAKE_ARGS='-DGGML_CUDA=on' sudo -E python3.11 -m pip install -r /opt/echopulpit/requirements.txt",

      # Pre-download/cache the faster-whisper model into the image so no
      # job pays that download cost.
      "python3.11 -c \"from faster_whisper import WhisperModel; WhisperModel('${var.whisper_model}', device='cpu', compute_type='int8')\"",

      "sudo mv /tmp/echopulpit-worker.service /etc/systemd/system/echopulpit-worker.service",
      "chmod +x /opt/echopulpit/bootstrap.sh",
      "sudo systemctl enable echopulpit-worker.service",
    ]
  }
}
