
class Disk:

	# Disk constructor
	#
	# @param diskfile	Name of the disk file to be opened. Should
	#					be a multiple of `blocksize` in size
	# @param blocksize	Block size to use
	#
	# @return none
	def __init__(self, diskfile, blocksize):

		try:
			self.diskfile = open(diskfile, "r+b")
		except IOError:
			raise Exception("Failed to open disk file")

		self.blocksize = blocksize
		self.blockreads = 0
		self.blockwrites = 0

	# Disk destructor
	#
	# @return none
	def __del__(self):

		self.diskfile.close()

	# Read a block from the disk
	#
	# @param n	Block number to read
	#
	# @return	`blocksize` number of bytes, from the block
	def readBlock(self, n):

		self.diskfile.seek(self.blocksize * n)
		data = self.diskfile.read(self.blocksize)

		self.blockreads += 1

		return data

	# Write a block to the disk
	#
	# @param n		Block number to write to
	# @param data	Data to be written to that block
	#
	# @return none
	def writeBlock(self, n, data):

		self.diskfile.seek(self.blocksize * n)
		self.diskfile.write(data)

		self.blockwrites += 1

	# Print statistics showing number of block read/writes
	#
	# @return none
	def printStats(self):

		print("")
		print("===== Disk usage statistics =====")
		print(" Total block reads:  {}".format(self.blockreads))
		print(" Total block writes: {}".format(self.blockwrites))
		print("=================================")
		print("")


