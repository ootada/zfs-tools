ROOT_DIR := $(shell dirname "$(realpath $(MAKEFILE_LIST))")

.PHONY = test install dist rpm srpm deb clean

test:
	cd $(ROOT_DIR) && \
	tox

install:
	if test x"$(DESTDIR)" = x; then echo "DESTDIR unset."; exit 1; fi
	mkdir -p $(DESTDIR)/usr/lib/python3/dist-packages
	mkdir -p $(DESTDIR)/usr/bin
	mkdir -p $(DESTDIR)/etc/sudoers.d
	python3 -m build -w
	unzip -q dist/zfs_tools-*.whl -d $(DESTDIR)/usr/lib/python3/dist-packages
	# Create symlinks for the console scripts
	for script in zbackup zflock zreplicate zsnap; do \
		ln -sf ../lib/python3/dist-packages/zfs_tools/$$script.py $(DESTDIR)/usr/bin/$$script; \
	done
	cp contrib/sudoers.zfs-tools $(DESTDIR)/etc/sudoers.d/zfs-shell
	chmod 440 $(DESTDIR)/etc/sudoers.d/zfs-shell

clean:
	cd $(ROOT_DIR) && find -name '*~' -print0 | xargs -0r rm -fv && rm -fr *.tar.gz *.rpm src/*.egg-info *.egg-info dist build

dist: clean
	cd $(ROOT_DIR) || exit $$? ; python3 -m build -s

srpm: dist
	@which rpmbuild || { echo 'rpmbuild is not available.  Please install the rpm-build package with the command `dnf install rpm-build` to continue, then rerun this step.' ; exit 1 ; }
	cd $(ROOT_DIR) || exit $$? ; rpmbuild --define "_srcrpmdir ." -ts dist/`rpmspec -q --queryformat 'zfs_tools-%{version}.tar.gz\n' *spec | head -1`

rpm: dist
	@which rpmbuild || { echo 'rpmbuild is not available.  Please install the rpm-build package with the command `dnf install rpm-build` to continue, then rerun this step.' ; exit 1 ; }
	cd $(ROOT_DIR) || exit $$? ; rpmbuild --define "_srcrpmdir ." --define "_rpmdir builddir.rpm" -ta dist/`rpmspec -q --queryformat 'zfs_tools-%{version}.tar.gz\n' *spec | head -1`
	cd $(ROOT_DIR) ; mv -f builddir.rpm/*/* . && rm -rf builddir.rpm

deb: dist
	@which dpkg-buildpackage || { echo 'dpkg-buildpackage is not available.  Please install the devscripts package with the command `apt-get install devscripts` to continue, then rerun this step.' ; exit 1 ; }
	cd $(ROOT_DIR) && dpkg-buildpackage -us -uc -b
