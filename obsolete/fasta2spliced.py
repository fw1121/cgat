################################################################################
#   Gene prediction pipeline 
#
#   $Id: fasta2spliced.py 2861 2010-02-23 17:36:32Z andreas $
#
#   Copyright (C) 2004 Andreas Heger
#
#   This program is free software; you can redistribute it and/or
#   modify it under the terms of the GNU General Public License
#   as published by the Free Software Foundation; either version 2
#   of the License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program; if not, write to the Free Software
#   Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#################################################################################
'''
fasta2spliced.py - extract splice junctions from fasta file
===========================================================

:Author: Andreas Heger
:Release: $Id$
:Date: |today|
:Tags: Python

Purpose
-------

Based on a set of exons and a genome assembly, this script
outputs the sequences and coordinates of all possible splice 
junctions.

This script was used to determine exon boundaries from very fragmentory
data. It is now obsolete.

Usage
-----

Example::

   python fasta2spliced.py --genome-file=hg19

Type::

   python <script_name>.py --help

for command line help.

Documentation
-------------

Code
----

'''

import sys
import string
import re
import optparse
import CGAT.Experiment as E
import CGAT.IndexedFasta as IndexedFasta
import CGAT.Genomics as Genomics

if __name__ == "__main__":

    parser = E.OptionParser( version = "%prog version: $Id: fasta2spliced.py 2861 2010-02-23 17:36:32Z andreas $")

    parser.add_option("-g", "--genome-file", dest="genome_file", type="string",
                      help="filename with genome."  )

    parser.add_option("-r", "--filename-regions", dest="filename_regions", type="string",
                      help="filename with region information in GFF format."  )

    parser.add_option( "-p", "--output-filename-pattern", dest="output_filename_pattern", type="string" ,
                       help="OUTPUT filename pattern for additional data [%default].")

    parser.add_option( "--joined", dest="joined", action="store_true",
                       help="output mode. If joined, all is output in one big chromosome. Otherwise, each are single fragments [%default].")

    parser.add_option( "--only-first", dest="only_first", action="store_true",
                       help="only output the first possible splice site [%default].")

    parser.set_defaults(
        genome_file = "genome",
        filename_regions = None,
        output_format = "%08i",
        output_filename_pattern = "%s",
        methods = [],
        splice_pairs = ( ("GT", "AG"), ),
        min_intron_size = 30,
        max_intron_size = 25000,
        search_area = 5, # 10
        read_length = 32,
        only_first = False,
        joined = False,
        max_join_length = 1000000, # 100000000
        format_id = "seg%05i",
        )

    (options, args) = E.Start( parser )

    genome = IndexedFasta.IndexedFasta( options.genome_file )

    assert options.filename_regions != None, "please supply a gff formatted filename with regions"

    regions = GTF.readAsIntervals( GFF.iterator( IOTools.openFile(options.filename_regions, "r" ) ) )

    # build pairs for complement
    reverse_splice_pairs = []
    forward_splice_pairs = options.splice_pairs
    left_tokens, right_tokens = {}, {}
    x = 0
    for a,b in forward_splice_pairs:
        assert len(a) == 2, "only two-residue patterns allowed"
        assert len(b) == 2, "only two-residue patterns allowed"

        ca, cb = Genomics.complement( a ), Genomics.complement( b ) 
        reverse_splice_pairs.append( (b,a) )
        left_tokens[a] = x
        left_tokens[cb] = x+1
        right_tokens[b] = x
        right_tokens[ca] = x+1
        x += 2

    search_area = options.search_area
    read_length = options.read_length
    joined = options.joined

    ninput, noutput = 0, 0

    if joined:
        outfile_coordinates = IOTools.openFile( options.output_filename_pattern % "coords", "w" )
        outfile_coordinates.write( "segment\tpos\tcontig\t5start\t3start\n" )
        out_contig = 1
        options.stdout.write( ">%s\n" % (options.format_id % out_contig ))
        nbases = 0
        separator = "N" * read_length
        lseparator = len(separator)

    contig_sizes = genome.getContigSizes()
    # collect possible start/end points of introns
    for contig, lcontig in contig_sizes.items():

        ninput += 1

        nintrons = 0
        if contig not in regions:
            E.debug( "skipped %s - no intervals defined" % (contig))
            continue

        sequence = genome.getSequence( contig, as_array = True )

        E.debug( "processing %s of length %i" % (contig, len(sequence)))

        regions[contig].sort()
        
        left_positions, right_positions = [], []

        def addPositions( start, end, tokens, positions, forward = True, first = False ):

            area = sequence[start:end].upper()
            if forward:
                for x in range(len(area)-1):
                    t = area[x:x+2]
                    if t in tokens: 
                        positions.append( (start+x,tokens[t] ) )
                        if first: return True
                    
            else:
                for x in range(len(area)-2,-1,-1):
                    t = area[x:x+2]
                    if t in tokens: 
                        positions.append( (start+x,tokens[t] ) )
                        if first: return True
            return False

        intron_start = regions[contig][0][1]
        for exon_start,exon_end in regions[contig][1:]:
            
            intron_end = exon_start
            if options.only_first:
                if not addPositions( intron_start, intron_start+search_area, left_tokens, left_positions, forward=True, first = True ):
                    addPositions( intron_start-search_area, intron_start, left_tokens, left_positions, forward=False, first = True )

                if not addPositions( intron_end-search_area, intron_end, right_tokens, right_positions, forward=False, first = True ):
                    addPositions( intron_end, intron_end+search_area, right_tokens, right_positions, forward=True, first = True )

            else:
                addPositions( intron_start-search_area, intron_start+search_area, left_tokens, left_positions, forward=True, first = False )
                addPositions( intron_end-search_area, intron_end+search_area, right_tokens, right_positions, forward=True, first = False )
            intron_start = exon_end

        E.debug("%s: left=%i, right=%i" % (contig, len(left_positions), len(right_positions) ))
        
        # build possible introns
        #
        # iterate over left positions and collect right positions within a radius
        # given by min_intron_size and max_intron_size.
        # left_positions and right_positions are sorted
        ri, mr = 0, len(right_positions)

        for l,t in left_positions:
            lower_bound, upper_bound = l + options.min_intron_size, l + options.max_intron_size
            while ri < mr and right_positions[ri][0] < lower_bound: ri += 1
            rri = ri

            while rri < mr and right_positions[rri][0] < upper_bound:
                if right_positions[rri][1] == t:
                    # positions are start/end of splice motif
                    # so add two on the right side
                    r = right_positions[rri][0]+2
                    lmin = max(0, l-read_length )
                    rmax = min( lcontig, r + read_length )

                    if options.loglevel >= 3:
                        options.stdlog.write("# adding intron on %s: l=%i, r=%i, t=%i, %s %s %s %s\n" %\
                                                 (contig, l, r, t, 
                                                  sequence[lmin:l], 
                                                  sequence[l:l+2], 
                                                  sequence[r-2:r], 
                                                  sequence[r:rmax] ) )
                        
                    if joined:
                        outfile_coordinates.write("%s\t%i\t%s\t%i\t%i\n" % (options.format_id % out_contig, nbases, contig, lmin, r ) )

                        s = sequence[lmin:l] + sequence[r:rmax]
                        options.stdout.write( "%s\n%s\n" % (s, separator ) )
                                              
                        nbases += len(s) + lseparator

                        if nbases > options.max_join_length:
                            nbases = 0
                            out_contig += 1
                            options.stdout.write( ">%s\n" % (options.format_id % out_contig ) )

                    else:
                        options.stdout.write( ">%s_%i_%i\n%s%s\n" % (contig, lmin, r,     
                                                                     sequence[lmin:l], 
                                                                     sequence[r:rmax] ) )

                    
                    nintrons += 1
                    noutput += 1
                rri += 1

        E.info( "contig %s: %i introns" % (contig, nintrons))
        
    E.info( "ninput=%i, noutput=%i" % (ninput, noutput) )

    E.Stop()
