# Supply a verified ROCm + Vivado/Vitis 2025.2 base image. No image digest is assumed.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
WORKDIR /opt/rtl_agent
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .
RUN chmod +x run.sh run_baseline.sh serve/start.sh serve/run_api.sh
ENV PYTHONPATH=/opt/rtl_agent HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUTF8=1 AGENT_TMP=/var/tmp/rtl_agent EDA_TMP=/var/tmp/rtl_eda RTL_AGENT_VIVADO_VERSION=2025.2
EXPOSE 7860
CMD ["/bin/bash"]
