# -*- coding: utf-8 -*-


# OpenFisca -- A versatile microsimulation software
# By: OpenFisca Team <contact@openfisca.fr>
#
# Copyright (C) 2011, 2012, 2013, 2014 OpenFisca Team
# https://github.com/openfisca
#
# This file is part of OpenFisca.
#
# OpenFisca is free software; you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# OpenFisca is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


from __future__ import division

import logging


from openfisca_core.baremes import BaremeDict, scaleBaremes
from openfisca_core.enumerations import Enum

TAUX_DE_PRIME = 1 / 4  # primes (hors supplément familial et indemnité de résidence) / rémunération brute


CAT = Enum(['prive_non_cadre',
            'prive_cadre',
            'public_titulaire_etat',
            'public_titulaire_militaire',
            'public_titulaire_territoriale',
            'public_titulaire_hospitaliere',
            'public_non_titulaire'])

DEBUG_SAL_TYPE = 'public_titulaire_hospitaliere'

from openfisca_core.legislations import CompactNode


log = logging.getLogger(__name__)

# TODO: contribution patronale de prévoyance complémentaire
# Formation professionnelle (entreprise de 10 à moins de 20 salariés)
#        salaire total 1,05%
# Formation professionnelle (entreprise de moins de 10 salariés)
#        salaire total 0,55%

def build_pat(_P):
    '''
    Construit le dictionnaire de barèmes des cotisations patronales
    à partir des informations contenues dans P.cotsoc.pat
    '''
    plaf_ss = 12 * _P.cotsoc.gen.plaf_ss
    pat = scaleBaremes(BaremeDict('pat', _P.cotsoc.pat), plaf_ss)
    pat['noncadre'].update(pat['commun'])
    pat['cadre'].update(pat['commun'])
    pat['fonc']['contract'].update(pat['commun'])

    # Renaiming
    pat['prive_non_cadre'] = pat.pop('noncadre')
    pat['prive_cadre'] = pat.pop('cadre')

    log.info(u"Le dictionnaire des barèmes des cotisations patronales des non cadres contient: \n %s", pat['prive_non_cadre'].keys())
    log.info(u"Le dictionnaire des barèmes des cotisations patronales des cadres contient: \n %s", pat['prive_cadre'].keys())

    # Rework commun to deal with public employees
    for var in ["maladie", "apprentissage", "apprentissage2", "vieillesseplaf", "vieillessedeplaf", "formprof", "chomfg", "construction", "assedic"]:
        del pat['commun'][var]

    for var in ["apprentissage", "apprentissage2", "formprof", "chomfg", "construction", "assedic"]:
        del pat['fonc']['contract'][var]

    pat['fonc']['etat'].update(pat['commun'])
    pat['fonc']['colloc'].update(pat['commun'])
    del pat['commun']

    pat['etat_t'] = pat['fonc']['etat']
    pat['colloc_t'] = pat['fonc']['colloc']
    pat['contract'] = pat['fonc']['contract']

    for var in ['etat', 'colloc', 'contract' ]:
        del pat['fonc'][var]

    # Renaiming
    pat['public_titulaire_etat'] = pat.pop('etat_t')
    del pat['public_titulaire_etat']['rafp']

    pat['public_titulaire_territoriale'] = pat.pop('colloc_t')
    import copy
    pat['public_titulaire_hospitaliere'] = copy.deepcopy(pat['public_titulaire_territoriale'])
    for category in ['territoriale', 'hospitaliere']:
        for name, bareme in pat['public_titulaire_' + category][category].iteritems():
            pat['public_titulaire_' + category][name] = bareme

    for category in ['territoriale', 'hospitaliere']:
        del pat['public_titulaire_territoriale'][category]
        del pat['public_titulaire_hospitaliere'][category]

    pat['public_non_titulaire'] = pat.pop('contract')
    log.info(u"Le dictionnaire des barèmes cotisations patronales %s contient : \n %s \n" % (DEBUG_SAL_TYPE, pat[DEBUG_SAL_TYPE].keys()))
    return pat

def build_sal(_P):
    '''
    Construit le dictionnaire de barèmes des cotisations salariales
    à partir des informations contenues dans P.cotsoc.sal
    '''
    plaf_ss = 12 * _P.cotsoc.gen.plaf_ss

    sal = scaleBaremes(BaremeDict('sal', _P.cotsoc.sal), plaf_ss)
    sal['noncadre'].update(sal['commun'])
    sal['cadre'].update(sal['commun'])

    # Renaiming
    sal['prive_non_cadre'] = sal.pop('noncadre')
    sal['prive_cadre'] = sal.pop('cadre')
    sal['public_titulaire_etat'] = sal['fonc']['etat']

    sal['public_titulaire_territoriale'] = sal['fonc']['colloc']
    sal['public_titulaire_hospitaliere'] = sal['fonc']['colloc']
    sal['public_non_titulaire'] = sal['fonc']['contract']

    for type_sal_category in ['public_titulaire_etat', 'public_titulaire_territoriale', 'public_titulaire_hospitaliere',
                               'public_non_titulaire']:
        sal[type_sal_category]['excep_solidarite'] = sal['fonc']['commun']['solidarite']

    sal['public_non_titulaire'].update(sal['commun'])
    del sal['public_non_titulaire']['arrco']
    del sal['public_non_titulaire']['assedic']

    # Cleaning
    del sal['commun']
    del sal['fonc']['etat']
    del sal['fonc']['colloc']
    del sal['fonc']['contract']

    log.info(u"Le dictionnaire des barèmes des salariés %s contient : \n %s \n" % (DEBUG_SAL_TYPE, sal[DEBUG_SAL_TYPE].keys()))

    return sal


def preprocess_legislation_parameters(legislation_parameters_list):
    '''
    Preprocess the legislation_parameters to build the cotisations sociales baremes
    '''

    for _P in legislation_parameters_list:

        sal = build_sal(_P)
        pat = build_pat(_P)

        _P.cotsoc.__dict__['cotisations_patronales'] = CompactNode()
        _P.cotsoc.__dict__['cotisations_salariales'] = CompactNode()

        for cotisation_name, bareme_dict in {'cotisations_patronales' : pat, 'cotisations_salariales': sal}.iteritems():
            for category in bareme_dict:
                if category in CAT._nums:
                    _P.cotsoc.__dict__[cotisation_name].__dict__[category] = bareme_dict[category]
