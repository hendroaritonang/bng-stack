VERSION ?= 1.0.0
ARCH    := amd64
DIST_DIR := dist

.PHONY: all runtime monitor repo clean

all: runtime monitor repo

runtime:
	@echo "→ Building bng-stack-runtime_$(VERSION)_$(ARCH).deb ..."
	chmod 755 packages/bng-stack-runtime/DEBIAN/postinst
	chmod 755 packages/bng-stack-runtime/DEBIAN/prerm
	chmod +x  packages/bng-stack-runtime/usr/local/sbin/bng-stack-configure
	chmod +x  packages/bng-stack-runtime/usr/local/sbin/bng-stack-start
	chmod +x  packages/bng-stack-runtime/usr/local/sbin/bng-stack-status
	chmod +x  packages/bng-stack-runtime/usr/local/sbin/accel-pppd
	chmod +x  packages/bng-stack-runtime/usr/local/sbin/accel-ppp-vpp-cleanup.sh
	chmod +x  packages/bng-stack-runtime/usr/local/sbin/pppoe-neigh-sync.py
	# Update version in control file
	sed -i "s/^Version:.*/Version: $(VERSION)/" packages/bng-stack-runtime/DEBIAN/control
	# Calculate installed size
	$(eval SIZE := $(shell du -sk packages/bng-stack-runtime | cut -f1))
	sed -i '/^Installed-Size:/d' packages/bng-stack-runtime/DEBIAN/control
	echo "Installed-Size: $(SIZE)" >> packages/bng-stack-runtime/DEBIAN/control
	mkdir -p $(DIST_DIR)
	dpkg-deb --build packages/bng-stack-runtime $(DIST_DIR)/bng-stack-runtime_$(VERSION)_$(ARCH).deb
	@echo "✓ Built $(DIST_DIR)/bng-stack-runtime_$(VERSION)_$(ARCH).deb"

monitor:
	@echo "→ Building bng-monitor_$(VERSION)_$(ARCH).deb ..."
	chmod 755 packages/bng-monitor/DEBIAN/postinst
	# Remove __pycache__ before packaging
	find packages/bng-monitor -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
	sed -i "s/^Version:.*/Version: $(VERSION)/" packages/bng-monitor/DEBIAN/control
	$(eval SIZE := $(shell du -sk packages/bng-monitor | cut -f1))
	sed -i '/^Installed-Size:/d' packages/bng-monitor/DEBIAN/control
	echo "Installed-Size: $(SIZE)" >> packages/bng-monitor/DEBIAN/control
	mkdir -p $(DIST_DIR)
	dpkg-deb --build packages/bng-monitor $(DIST_DIR)/bng-monitor_$(VERSION)_$(ARCH).deb
	@echo "✓ Built $(DIST_DIR)/bng-monitor_$(VERSION)_$(ARCH).deb"

repo: runtime monitor
	@echo "→ Building apt repository in $(DIST_DIR)/apt-repo/ ..."
	mkdir -p $(DIST_DIR)/apt-repo/pool/main/b/bng-stack-runtime
	mkdir -p $(DIST_DIR)/apt-repo/pool/main/b/bng-monitor
	mkdir -p $(DIST_DIR)/apt-repo/dists/stable/main/binary-amd64
	cp $(DIST_DIR)/bng-stack-runtime_$(VERSION)_$(ARCH).deb \
	   $(DIST_DIR)/apt-repo/pool/main/b/bng-stack-runtime/
	cp $(DIST_DIR)/bng-monitor_$(VERSION)_$(ARCH).deb \
	   $(DIST_DIR)/apt-repo/pool/main/b/bng-monitor/
	# Generate Packages index
	cd $(DIST_DIR)/apt-repo && \
	  dpkg-scanpackages pool/main /dev/null > dists/stable/main/binary-amd64/Packages 2>/dev/null && \
	  gzip -9c dists/stable/main/binary-amd64/Packages > dists/stable/main/binary-amd64/Packages.gz
	# Generate Release file
	cd $(DIST_DIR)/apt-repo && \
	  apt-ftparchive release dists/stable > dists/stable/Release 2>/dev/null || \
	  (echo "Origin: bng-stack"; echo "Label: bng-stack"; echo "Suite: stable"; \
	   echo "Codename: stable"; echo "Architectures: amd64"; \
	   echo "Components: main"; echo "Description: BNG Stack apt repository") > dists/stable/Release
	# Copy installer
	cp installer/install.sh $(DIST_DIR)/apt-repo/install.sh
	@echo "✓ apt repository built in $(DIST_DIR)/apt-repo/"

clean:
	rm -rf $(DIST_DIR)
	find packages -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
