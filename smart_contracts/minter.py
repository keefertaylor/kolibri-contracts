import smartpy as sp

Addresses = sp.import_script_from_url("file:test-helpers/addresses.py")
Constants = sp.import_script_from_url("file:common/constants.py")
Errors = sp.import_script_from_url("file:common/errors.py")
OvenApi = sp.import_script_from_url("file:common/oven-api.py")

################################################################
# Contract
################################################################

class MinterContract(sp.Contract):
    def __init__(
        self,
        tokenContractAddress = Addresses.TOKEN_ADDRESS,
        governorContractAddress = Addresses.GOVERNOR_ADDRESS,  
        ovenProxyContractAddress = Addresses.OVEN_PROXY_ADDRESS,
        stabilityFundContractAddress = Addresses.STABILITY_FUND_ADDRESS,
        developerFundContractAddress = Addresses.DEVELOPER_FUND_ADDRESS,
        collateralizationPercentage = sp.nat(200000000000000000000), # 200%
        stabilityFee = sp.nat(0),
        lastInterestIndexUpdateTime = sp.timestamp(1601871456),
        interestIndex = 1000000000000000000,
        stabilityDevFundSplit = sp.nat(100000000000000000), # 10%
        liquidationFeePercent = sp.nat(80000000000000000),  # 8%
        ovenMax = sp.some(sp.tez(100))
    ):
        self.exception_optimization_level = "DefaultUnit"
        self.add_flag("no_comment")

        self.init(
            governorContractAddress = governorContractAddress,
            tokenContractAddress = tokenContractAddress,
            ovenProxyContractAddress = ovenProxyContractAddress,
            collateralizationPercentage = collateralizationPercentage,
            developerFundContractAddress = developerFundContractAddress,
            stabilityFundContractAddress = stabilityFundContractAddress,
            liquidationFeePercent = liquidationFeePercent,
            stabilityDevFundSplit = stabilityDevFundSplit,
            ovenMax = ovenMax,
 
            # Interest Calculations
            interestIndex = interestIndex,
            stabilityFee = stabilityFee,
            lastInterestIndexUpdateTime = lastInterestIndexUpdateTime,
        )

    ################################################################
    # Public Interface
    ################################################################

    # Get interest index
    @sp.entry_point
    def getInterestIndex(self, param):        
        sp.set_type(param, sp.TContract(sp.TNat))

        # Verify the call did not contain a balance.
        sp.verify(sp.amount == sp.mutez(0), message = Errors.AMOUNT_NOT_ALLOWED)

        # Compound interest
        timeDeltaSeconds = sp.as_nat(sp.now - self.data.lastInterestIndexUpdateTime)
        numPeriods = timeDeltaSeconds // Constants.SECONDS_PER_COMPOUND
        newMinterInterestIndex = self.compoundWithLinearApproximation((self.data.interestIndex, (self.data.stabilityFee, numPeriods)))

        # Transfer results to requester.
        sp.transfer(newMinterInterestIndex, sp.mutez(0), param)

        # Update internal state.
        self.data.interestIndex = newMinterInterestIndex
        self.data.lastInterestIndexUpdateTime = self.data.lastInterestIndexUpdateTime.add_seconds(sp.to_int(numPeriods * Constants.SECONDS_PER_COMPOUND))

    ################################################################
    # Oven Interface
    ################################################################

    # borrow
    @sp.entry_point
    def borrow(self, param):
        sp.set_type(param, OvenApi.BORROW_PARAMETER_TYPE_ORACLE)

        # Verify the sender is the oven proxy.
        sp.verify(sp.sender == self.data.ovenProxyContractAddress, message = Errors.NOT_OVEN_PROXY)

        # Destructure input params.        
        oraclePrice,           pair1 = sp.match_pair(param)
        ovenAddress,           pair2 = sp.match_pair(pair1)
        ownerAddress,          pair3 = sp.match_pair(pair2)
        ovenBalance,           pair4 = sp.match_pair(pair3)
        borrowedTokens,        pair5 = sp.match_pair(pair4)
        isLiquidated,          pair6 = sp.match_pair(pair5)
        stabilityFeeTokensInt, pair7 = sp.match_pair(pair6)
        interestIndex                = sp.fst(pair7)
        tokensToBorrow               = sp.snd(pair7)

        stabilityFeeTokens = sp.as_nat(stabilityFeeTokensInt)

        sp.set_type(oraclePrice, sp.TNat)
        sp.set_type(ovenAddress, sp.TAddress)
        sp.set_type(ownerAddress, sp.TAddress)
        sp.set_type(ovenBalance, sp.TNat)
        sp.set_type(borrowedTokens, sp.TNat)
        sp.set_type(isLiquidated, sp.TBool)
        sp.set_type(stabilityFeeTokens, sp.TNat)
        sp.set_type(interestIndex, sp.TInt)
        sp.set_type(tokensToBorrow, sp.TNat)

        # Calculate new interest indices for the minter and the oven.
        timeDeltaSeconds = sp.as_nat(sp.now - self.data.lastInterestIndexUpdateTime)
        numPeriods = timeDeltaSeconds // Constants.SECONDS_PER_COMPOUND
        newMinterInterestIndex = self.compoundWithLinearApproximation((self.data.interestIndex, (self.data.stabilityFee, numPeriods)))

        # Disallow repay operations on liquidated ovens.
        sp.verify(isLiquidated == False, message = Errors.LIQUIDATED)

        # Calculate newly accrued stability fees and determine total fees.
        accruedStabilityFeeTokens = self.calculateNewAccruedInterest((interestIndex, (borrowedTokens, (stabilityFeeTokens, (newMinterInterestIndex)))))
        newStabilityFeeTokens = stabilityFeeTokens + accruedStabilityFeeTokens

        # Compute new borrowed amount.
        newTotalBorrowedTokens = borrowedTokens + tokensToBorrow
        sp.set_type(newTotalBorrowedTokens, sp.TNat)

        # Verify the oven is not under-collateralized. 
        totalOutstandingTokens = newTotalBorrowedTokens + newStabilityFeeTokens
        sp.set_type(totalOutstandingTokens, sp.TNat)
        sp.if totalOutstandingTokens > 0:
            newCollateralizationPercentage = self.computeCollateralizationPercentage((ovenBalance, (oraclePrice, totalOutstandingTokens)))
            sp.verify(newCollateralizationPercentage >= self.data.collateralizationPercentage, message = Errors.OVEN_UNDER_COLLATERALIZED)

        # Call mint in token contract
        self.mintTokens(tokensToBorrow, ownerAddress)

        # Inform oven of new state.
        self.updateOvenState(ovenAddress, newTotalBorrowedTokens, newStabilityFeeTokens, newMinterInterestIndex, isLiquidated, sp.balance)

        # Update internal state
        self.data.interestIndex = newMinterInterestIndex
        self.data.lastInterestIndexUpdateTime = self.data.lastInterestIndexUpdateTime.add_seconds(sp.to_int(numPeriods * Constants.SECONDS_PER_COMPOUND))

    # repay
    @sp.entry_point
    def repay(self, param):
        sp.set_type(param, OvenApi.REPAY_PARAMETER_TYPE)

        # Verify the sender is the oven proxy.
        sp.verify(sp.sender == self.data.ovenProxyContractAddress, message = Errors.NOT_OVEN_PROXY)

        # Destructure input params.        
        ovenAddress,           pair1 = sp.match_pair(param)
        ownerAddress,          pair2 = sp.match_pair(pair1)
        ovenBalance,           pair3 = sp.match_pair(pair2)
        borrowedTokens,        pair4 = sp.match_pair(pair3)
        isLiquidated,          pair5 = sp.match_pair(pair4)
        stabilityFeeTokensInt, pair6 = sp.match_pair(pair5)
        interestIndex                = sp.fst(pair6)
        tokensToRepay                = sp.snd(pair6)

        stabilityFeeTokens = sp.as_nat(stabilityFeeTokensInt)

        sp.set_type(ovenAddress, sp.TAddress)
        sp.set_type(ownerAddress, sp.TAddress)
        sp.set_type(ovenBalance, sp.TNat)
        sp.set_type(borrowedTokens, sp.TNat)
        sp.set_type(isLiquidated, sp.TBool)
        sp.set_type(stabilityFeeTokens, sp.TNat)
        sp.set_type(interestIndex, sp.TInt)
        sp.set_type(tokensToRepay, sp.TNat)

        # Calculate new interest indices for the minter and the oven.
        timeDeltaSeconds = sp.as_nat(sp.now - self.data.lastInterestIndexUpdateTime)
        numPeriods = timeDeltaSeconds // Constants.SECONDS_PER_COMPOUND
        newMinterInterestIndex = self.compoundWithLinearApproximation((self.data.interestIndex, (self.data.stabilityFee, numPeriods)))

        # Disallow repay operations on liquidated ovens.
        sp.verify(isLiquidated == False, message = Errors.LIQUIDATED)

        # Calculate newly accrued stability fees and determine total fees.
        accruedStabilityFeeTokens = self.calculateNewAccruedInterest((interestIndex, (borrowedTokens, (stabilityFeeTokens, (newMinterInterestIndex)))))
        newStabilityFeeTokens = stabilityFeeTokens + accruedStabilityFeeTokens

        # Determine new values for stability fee tokens and borrowed token value. 
        # Also, note down the number of stability fee tokens repaid.
        stabilityFeeTokensRepaid = sp.local("stabilityFeeTokensRepaid", 0)
        remainingStabilityFeeTokens = sp.local("remainingStabilityFeeTokens", 0)
        remainingBorrowedTokenBalance = sp.local("remainingBorrowedTokenBalance", 0)
        sp.if tokensToRepay < newStabilityFeeTokens:
            stabilityFeeTokensRepaid.value = tokensToRepay
            remainingStabilityFeeTokens.value = sp.as_nat(newStabilityFeeTokens - tokensToRepay)
            remainingBorrowedTokenBalance.value = borrowedTokens
        sp.else:
            stabilityFeeTokensRepaid.value = newStabilityFeeTokens
            remainingStabilityFeeTokens.value = sp.nat(0)
            remainingBorrowedTokenBalance.value = sp.as_nat(borrowedTokens - sp.as_nat(tokensToRepay - newStabilityFeeTokens))

        # Burn and mint tokens in Dev fund.
        self.mintTokensToStabilityAndDevFund(stabilityFeeTokensRepaid.value)
        self.burnTokens(tokensToRepay, ownerAddress)

        # Inform oven of new state.
        self.updateOvenState(ovenAddress, remainingBorrowedTokenBalance.value, remainingStabilityFeeTokens.value, newMinterInterestIndex, isLiquidated, sp.balance)

        # Update internal state
        self.data.interestIndex = newMinterInterestIndex
        self.data.lastInterestIndexUpdateTime = self.data.lastInterestIndexUpdateTime.add_seconds(sp.to_int(numPeriods * Constants.SECONDS_PER_COMPOUND))

    # deposit
    @sp.entry_point
    def deposit(self, param):
        sp.set_type(param, OvenApi.DEPOSIT_PARAMETER_TYPE)

        # Verify the sender is a oven.
        sp.verify(sp.sender == self.data.ovenProxyContractAddress, message = Errors.NOT_OVEN_PROXY)

        # Verify the balance did not exceed the threshold.
        sp.if self.data.ovenMax.is_some():
            sp.verify(sp.balance <= self.data.ovenMax.open_some(), Errors.OVEN_MAXIMUM_EXCEEDED)

        # Destructure input params.        
        ovenAddress,           pair1 = sp.match_pair(param)
        ownerAddress,          pair2 = sp.match_pair(pair1)
        ovenBalance,           pair3 = sp.match_pair(pair2)
        borrowedTokens,        pair4 = sp.match_pair(pair3)
        isLiquidated,          pair5 = sp.match_pair(pair4)
        stabilityFeeTokensInt        = sp.fst(pair5)
        interestIndex                = sp.snd(pair5)

        stabilityFeeTokens = sp.as_nat(stabilityFeeTokensInt)

        sp.set_type(ovenAddress, sp.TAddress)
        sp.set_type(ownerAddress, sp.TAddress)
        sp.set_type(ovenBalance, sp.TNat)
        sp.set_type(borrowedTokens, sp.TNat)
        sp.set_type(isLiquidated, sp.TBool)
        sp.set_type(stabilityFeeTokens, sp.TNat)
        sp.set_type(interestIndex, sp.TInt)

        # Calculate new interest indices for the minter and the oven.
        timeDeltaSeconds = sp.as_nat(sp.now - self.data.lastInterestIndexUpdateTime)
        numPeriods = timeDeltaSeconds // Constants.SECONDS_PER_COMPOUND
        newMinterInterestIndex = self.compoundWithLinearApproximation((self.data.interestIndex, (self.data.stabilityFee, numPeriods)))

        # Disallow deposit operations on liquidated ovens.
        sp.verify(isLiquidated == False, message = Errors.LIQUIDATED)

        # Calculate newly accrued stability fees and determine total fees.
        accruedStabilityFeeTokens = self.calculateNewAccruedInterest((interestIndex, (borrowedTokens, (stabilityFeeTokens, (newMinterInterestIndex)))))
        newStabilityFeeTokens = stabilityFeeTokens + accruedStabilityFeeTokens

        # Intentional no-op. Pass value back to oven.
        self.updateOvenState(ovenAddress, borrowedTokens, newStabilityFeeTokens, newMinterInterestIndex, isLiquidated, sp.balance)
        
        # Update internal state
        self.data.interestIndex = newMinterInterestIndex
        self.data.lastInterestIndexUpdateTime = self.data.lastInterestIndexUpdateTime.add_seconds(sp.to_int(numPeriods * Constants.SECONDS_PER_COMPOUND))

    @sp.entry_point
    def withdraw(self, param):
        sp.set_type(param, OvenApi.WITHDRAW_PARAMETER_TYPE_ORACLE)

        # Verify the sender is a oven.
        sp.verify(sp.sender == self.data.ovenProxyContractAddress, message = Errors.NOT_OVEN_PROXY)

        # Destructure input params.        
        oraclePrice,           pair1 = sp.match_pair(param)
        ovenAddress,           pair2 = sp.match_pair(pair1)
        ownerAddress,          pair3 = sp.match_pair(pair2)
        ovenBalance,           pair4 = sp.match_pair(pair3)
        borrowedTokens,        pair5 = sp.match_pair(pair4)
        isLiquidated,          pair6 = sp.match_pair(pair5)
        stabilityFeeTokensInt, pair7 = sp.match_pair(pair6)
        interestIndex                = sp.fst(pair7)
        mutezToWithdraw              = sp.snd(pair7)

        stabilityFeeTokens = sp.as_nat(stabilityFeeTokensInt)

        sp.set_type(oraclePrice, sp.TNat)
        sp.set_type(ovenAddress, sp.TAddress)
        sp.set_type(ownerAddress, sp.TAddress)
        sp.set_type(ovenBalance, sp.TNat)
        sp.set_type(borrowedTokens, sp.TNat)
        sp.set_type(isLiquidated, sp.TBool)
        sp.set_type(stabilityFeeTokens, sp.TNat)
        sp.set_type(interestIndex, sp.TInt)
        sp.set_type(mutezToWithdraw, sp.TMutez)

        # Calculate new interest indices for the minter and the oven.
        timeDeltaSeconds = sp.as_nat(sp.now - self.data.lastInterestIndexUpdateTime)
        numPeriods = timeDeltaSeconds // Constants.SECONDS_PER_COMPOUND
        newMinterInterestIndex = self.compoundWithLinearApproximation((self.data.interestIndex, (self.data.stabilityFee, numPeriods)))

        # Calculate newly accrued stability fees and determine total fees.
        accruedStabilityFeeTokens = self.calculateNewAccruedInterest((interestIndex, (borrowedTokens, (stabilityFeeTokens, (newMinterInterestIndex)))))
        newStabilityFeeTokens = stabilityFeeTokens + accruedStabilityFeeTokens

        # Verify the oven has not become under-collateralized.
        totalOutstandingTokens = borrowedTokens + newStabilityFeeTokens
        sp.if totalOutstandingTokens > 0:
            withdrawAmount = sp.fst(sp.ediv(mutezToWithdraw, sp.mutez(1)).open_some()) * Constants.MUTEZ_TO_KOLIBRI_CONVERSION
            newOvenBalance = sp.as_nat(ovenBalance - withdrawAmount)
            newCollateralizationPercentage = self.computeCollateralizationPercentage((newOvenBalance, (oraclePrice, totalOutstandingTokens))) 
            sp.verify(newCollateralizationPercentage >= self.data.collateralizationPercentage, message = Errors.OVEN_UNDER_COLLATERALIZED)

        # Withdraw mutez to the owner.
        sp.send(ownerAddress, mutezToWithdraw)

        # Update the oven's state and return the remaining mutez to it.
        remainingMutez = sp.mutez(ovenBalance // Constants.MUTEZ_TO_KOLIBRI_CONVERSION) - mutezToWithdraw
        self.updateOvenState(ovenAddress, borrowedTokens, newStabilityFeeTokens, newMinterInterestIndex, isLiquidated, remainingMutez)
        
        # Update internal state
        self.data.interestIndex = newMinterInterestIndex
        self.data.lastInterestIndexUpdateTime = self.data.lastInterestIndexUpdateTime.add_seconds(sp.to_int(numPeriods * Constants.SECONDS_PER_COMPOUND))

    # liquidate
    @sp.entry_point
    def liquidate(self, param):
        sp.set_type(param, OvenApi.LIQUIDATE_PARAMETER_TYPE_ORACLE)

        # Verify the sender is a oven.
        sp.verify(sp.sender == self.data.ovenProxyContractAddress, message = Errors.NOT_OVEN_PROXY)

        # Destructure input params.        
        oraclePrice,           pair1 = sp.match_pair(param)
        ovenAddress,           pair2 = sp.match_pair(pair1)
        ownerAddress,          pair3 = sp.match_pair(pair2)
        ovenBalance,           pair4 = sp.match_pair(pair3)
        borrowedTokens,        pair5 = sp.match_pair(pair4)
        isLiquidated,          pair6 = sp.match_pair(pair5)
        stabilityFeeTokensInt, pair7 = sp.match_pair(pair6)
        interestIndex                = sp.fst(pair7)
        liquidatorAddress            = sp.snd(pair7)

        stabilityFeeTokens = sp.as_nat(stabilityFeeTokensInt)

        sp.set_type(oraclePrice, sp.TNat)
        sp.set_type(ovenAddress, sp.TAddress)
        sp.set_type(ownerAddress, sp.TAddress)
        sp.set_type(ovenBalance, sp.TNat)
        sp.set_type(borrowedTokens, sp.TNat)
        sp.set_type(isLiquidated, sp.TBool)
        sp.set_type(stabilityFeeTokens, sp.TNat)
        sp.set_type(interestIndex, sp.TInt)
        sp.set_type(liquidatorAddress, sp.TAddress)

        # Calculate new interest indices for the minter and the oven.
        timeDeltaSeconds = sp.as_nat(sp.now - self.data.lastInterestIndexUpdateTime)
        numPeriods = timeDeltaSeconds // Constants.SECONDS_PER_COMPOUND
        newMinterInterestIndex = self.compoundWithLinearApproximation((self.data.interestIndex, (self.data.stabilityFee, numPeriods)))

        # Disallow additional liquidate operations on liquidated ovens.
        sp.verify(isLiquidated == False, message = Errors.LIQUIDATED)

        # Calculate newly accrued stability fees and determine total fees.
        accruedStabilityFeeTokens = self.calculateNewAccruedInterest((interestIndex, (borrowedTokens, (stabilityFeeTokens, (newMinterInterestIndex)))))
        newStabilityFeeTokens = stabilityFeeTokens + accruedStabilityFeeTokens

        # Verify collateral percentage.
        totalOutstandingTokens = borrowedTokens + newStabilityFeeTokens
        collateralizationPercentage = self.computeCollateralizationPercentage((ovenBalance, (oraclePrice, totalOutstandingTokens)))
        sp.verify(collateralizationPercentage < self.data.collateralizationPercentage, message = Errors.NOT_UNDER_COLLATERALIZED)

        # Calculate a liquidation fee.
        liquidationFee = (totalOutstandingTokens * self.data.liquidationFeePercent) // Constants.PRECISION

        # Burn tokens from the liquidator to pay for the Oven.
        self.burnTokens((totalOutstandingTokens + liquidationFee), liquidatorAddress)
                
        # Mint the extra tokens in the dev fund if they were paid.
        self.mintTokensToStabilityAndDevFund(newStabilityFeeTokens + liquidationFee)

        # Send collateral to liquidator.
        sp.send(liquidatorAddress, sp.mutez(ovenBalance // Constants.MUTEZ_TO_KOLIBRI_CONVERSION))

        # Inform oven it is liquidated, clear owed tokens and return no collateral.
        self.updateOvenState(ovenAddress, sp.nat(0), sp.nat(0), newMinterInterestIndex, True, sp.mutez(0))

        # Update internal state
        self.data.interestIndex = newMinterInterestIndex
        self.data.lastInterestIndexUpdateTime = self.data.lastInterestIndexUpdateTime.add_seconds(sp.to_int(numPeriods * Constants.SECONDS_PER_COMPOUND))

    ################################################################
    # Governance
    #
    # Some of these are lumped together to avoid excessive contract
    # size.
    ################################################################

    # Params: (stabilityFee, (liquidationFeePercent, (collateralizationPercentage, ovenMax)))
    @sp.entry_point
    def updateParams(self, newParams):
        sp.set_type(newParams, sp.TPair(sp.TNat, sp.TPair(sp.TNat, sp.TPair(sp.TNat, sp.TOption(sp.TMutez)))))

        sp.verify(sp.sender == self.data.governorContractAddress, message = Errors.NOT_GOVERNOR)

        # Compound interest and update internal state.
        timeDeltaSeconds = sp.as_nat(sp.now - self.data.lastInterestIndexUpdateTime)
        numPeriods = timeDeltaSeconds // Constants.SECONDS_PER_COMPOUND
        newMinterInterestIndex = self.compoundWithLinearApproximation((self.data.interestIndex, (self.data.stabilityFee, numPeriods)))
        self.data.interestIndex = newMinterInterestIndex
        self.data.lastInterestIndexUpdateTime = self.data.lastInterestIndexUpdateTime.add_seconds(sp.to_int(numPeriods * Constants.SECONDS_PER_COMPOUND))

        # Update to new parameters.
        newStabilityFee, pair1                     = sp.match_pair(newParams)
        newLiquidationFeePercent, pair2            = sp.match_pair(pair1)
        newCollateralizationPercentage, newOvenMax = sp.match_pair(pair2)

        self.data.stabilityFee                = newStabilityFee
        self.data.liquidationFeePercent       = newLiquidationFeePercent
        self.data.collateralizationPercentage = newCollateralizationPercentage
        self.data.ovenMax                     = newOvenMax

    # Params: (governor (token, (ovenProxy, (stabilityFund, devFund))))
    @sp.entry_point
    def updateContracts(self, newParams):
        sp.set_type(newParams, sp.TPair(sp.TAddress, sp.TPair(sp.TAddress, sp.TPair(sp.TAddress, sp.TPair(sp.TAddress, sp.TAddress)))))

        sp.verify(sp.sender == self.data.governorContractAddress, message = Errors.NOT_GOVERNOR)

        newGovernorContractAddress, pair1                                = sp.match_pair(newParams)
        newTokenContractAddress, pair2                                   = sp.match_pair(pair1)
        newOvenProxyContractAddress, pair3                               = sp.match_pair(pair2)
        newStabilityFundContractAddress, newDeveloperFundContractAddress = sp.match_pair(pair3)

        self.data.governorContractAddress      = newGovernorContractAddress
        self.data.tokenContractAddress         = newTokenContractAddress
        self.data.ovenProxyContractAddress     = newOvenProxyContractAddress       
        self.data.stabilityFundContractAddress = newStabilityFundContractAddress
        self.data.developerFundContractAddress = newDeveloperFundContractAddress

    ################################################################
    # Helpers
    ################################################################

    # Mint tokens to a stability fund.
    # 
    # This function does *NOT* burn tokens - the caller must do this. This is an efficiency gain because normally
    # we would burn tokens for collateral + stability fees.
    def mintTokensToStabilityAndDevFund(self, tokensToMint):
        sp.set_type(tokensToMint, sp.TNat)

        # Determine proportion of tokens minted to dev fund.
        tokensForDevFund = (tokensToMint * self.data.stabilityDevFundSplit) // Constants.PRECISION
        tokensForStabilityFund = sp.as_nat(tokensToMint - tokensForDevFund)

        # Mint tokens
        self.mintTokens(tokensForDevFund, self.data.developerFundContractAddress)
        self.mintTokens(tokensForStabilityFund, self.data.stabilityFundContractAddress)

    def burnTokens(self, tokensToBurn, address):
        sp.set_type(tokensToBurn, sp.TNat)
        sp.set_type(address, sp.TAddress)

        tokenContractParam = sp.record(address= address, value= tokensToBurn)
        contractHandle = sp.contract(
            sp.TRecord(address = sp.TAddress, value = sp.TNat),
            self.data.tokenContractAddress,
            "burn"
        ).open_some()
        sp.transfer(tokenContractParam, sp.mutez(0), contractHandle)

    def mintTokens(self, tokensToMint, address):
        sp.set_type(tokensToMint, sp.TNat)
        sp.set_type(address, sp.TAddress)

        tokenContractParam = sp.record(address= address, value= tokensToMint)
        contractHandle = sp.contract(
            sp.TRecord(address = sp.TAddress, value = sp.TNat),
            self.data.tokenContractAddress,
            "mint"
        ).open_some()
        sp.transfer(tokenContractParam, sp.mutez(0), contractHandle)

    # Calculate newly accrued stability fees with the given input.
    @sp.global_lambda
    def calculateNewAccruedInterest(params):
        sp.set_type(params, sp.TPair(sp.TInt, sp.TPair(sp.TNat, sp.TPair(sp.TNat, sp.TNat))))

        ovenInterestIndex =  sp.as_nat(sp.fst(params))
        borrowedTokens =     sp.fst(sp.snd(params))
        stabilityFeeTokens = sp.fst(sp.snd(sp.snd(params)))
        minterInterestIndex = sp.snd(sp.snd(sp.snd(params)))

        ratio = sp.fst(sp.ediv((minterInterestIndex * Constants.PRECISION), ovenInterestIndex).open_some())        
        totalPrinciple = borrowedTokens + stabilityFeeTokens
        newTotalTokens = sp.fst(sp.ediv((ratio * totalPrinciple), Constants.PRECISION).open_some())
        newTokensAccruedAsFee = sp.as_nat(newTotalTokens - totalPrinciple)
        sp.result(newTokensAccruedAsFee)

    # Compound interest via a linear approximation.
    @sp.global_lambda
    def compoundWithLinearApproximation(params):
        sp.set_type(params, sp.TPair(sp.TNat, sp.TPair(sp.TNat, sp.TNat)))

        initialValue = sp.fst(params)
        stabilityFee = sp.fst(sp.snd(params))
        numPeriods = sp.snd(sp.snd(params))

        sp.result((initialValue * (Constants.PRECISION + (numPeriods * stabilityFee))) // Constants.PRECISION)

    # Compute the collateralization percentage from the given inputs
    # Output is in the form of 200_000_000 (= 200%)
    @sp.global_lambda
    def computeCollateralizationPercentage(params):
        sp.set_type(params, sp.TPair(sp.TNat, sp.TPair(sp.TNat, sp.TNat)))

        ovenBalance = sp.fst(params)
        xtzPrice = sp.fst(sp.snd(params))
        borrowedTokens = sp.snd(sp.snd(params))

        # Compute collateral value.
        collateralValue = ovenBalance * xtzPrice // Constants.PRECISION
        ratio = (collateralValue * Constants.PRECISION) // (borrowedTokens)
        sp.result(ratio * 100)

    def updateOvenState(self, ovenAddress, borrowedTokens, stabilityFeeTokens, interestIndex, isLiquidated, sendAmount):
        sp.set_type(ovenAddress, sp.TAddress)
        sp.set_type(borrowedTokens, sp.TNat)
        sp.set_type(stabilityFeeTokens, sp.TNat)
        sp.set_type(interestIndex, sp.TNat)
        sp.set_type(isLiquidated, sp.TBool)
        sp.set_type(sendAmount, sp.TMutez)

        # Inform oven of new state.
        ovenContractParam = (ovenAddress, (borrowedTokens, (sp.to_int(stabilityFeeTokens), (sp.to_int(interestIndex), isLiquidated))))

        ovenHandle = sp.contract(
            OvenApi.UPDATE_STATE_PARAMETER_TYPE,
            self.data.ovenProxyContractAddress,
            OvenApi.UPDATE_STATE_ENTRY_POINT_NAME
        ).open_some()

        sp.transfer(ovenContractParam, sendAmount, ovenHandle)

# Only run tests if this file is main.
if __name__ == "__main__":

    ################################################################
    ################################################################
    # Tests
    ################################################################
    ################################################################

    Addresses = sp.import_script_from_url("file:test-helpers/addresses.py")
    DevFund = sp.import_script_from_url("file:dev-fund.py")
    DummyContract = sp.import_script_from_url("file:test-helpers/dummy-contract.py")
    MockOvenProxy = sp.import_script_from_url("file:test-helpers/mock-oven-proxy.py")
    StabilityFund = sp.import_script_from_url("file:stability-fund.py")
    Token = sp.import_script_from_url("file:token.py")

    ################################################################
    # Helpers
    ################################################################

    # A Tester flass that wraps a lambda function to allow for unit testing.
    # See: https://smartpy.io/releases/20201220-f9f4ad18bd6ec2293f22b8c8812fefbde46d6b7d/ide?template=test_global_lambda.py
    class Tester(sp.Contract):
        def __init__(self, lambdaFunc):
            self.lambdaFunc = sp.inline_result(lambdaFunc.f)
            self.init(lambdaResult = sp.none)

        @sp.entry_point
        def test(self, params):
            self.data.lambdaResult = sp.some(self.lambdaFunc(params))

        @sp.entry_point
        def check(self, params, result):
            sp.verify(self.lambdaFunc(params) == result)

    ################################################################
    # calculateNewAccruedInterest
    ################################################################

    @sp.add_test(name="calculateNewAccruedInterest")
    def test():
        scenario = sp.test_scenario()
        minter = MinterContract()
        scenario += minter

        tester = Tester(minter.calculateNewAccruedInterest)
        scenario += tester

        scenario += tester.check(
            params = (sp.to_int(1 * Constants.PRECISION), (100 * Constants.PRECISION, (0 * Constants.PRECISION,  1100000000000000000))), 
            result = 10 * Constants.PRECISION
        )
        scenario += tester.check(
            params = (sp.int(1100000000000000000), (100 * Constants.PRECISION, (10 * Constants.PRECISION, 1210000000000000000))),
            result = 11 * Constants.PRECISION
        )
        scenario += tester.check(
            params = (sp.to_int(1 * Constants.PRECISION), (100 * Constants.PRECISION, (0 * Constants.PRECISION,  1210000000000000000))),
            result = 21 * Constants.PRECISION
        )

        scenario += tester.check(
            params = (sp.int(1100000000000000000), (100 * Constants.PRECISION, (0 * Constants.PRECISION,  1210000000000000000))),
            result = 10 * Constants.PRECISION
        )
        scenario += tester.check(
            params = (sp.int(1210000000000000000), (100 * Constants.PRECISION, (10 * Constants.PRECISION, 1331000000000000000))),
            result = 11 * Constants.PRECISION
        )
        scenario += tester.check(
            params = (sp.int(1100000000000000000), (100 * Constants.PRECISION, (0 * Constants.PRECISION,  1331000000000000000))),
            result = 21 * Constants.PRECISION
        )
        
    ################################################################
    # compoundWithLinearApproximation
    ################################################################
        
    @sp.add_test(name="compoundWithLinearApproximation")
    def test():
        scenario = sp.test_scenario()
        minter = MinterContract()
        scenario += minter

        tester = Tester(minter.compoundWithLinearApproximation)
        scenario += tester

        # Two periods back to back
        scenario += tester.check(
            params = (1 * Constants.PRECISION, (100000000000000000, 1)), 
            result = sp.nat(1100000000000000000)
        )
        scenario += tester.check(
            params = (1100000000000000000,  (100000000000000000, 1)), 
            result = 1210000000000000000
        )

        # Two periods in one update
        scenario += tester.check(
            params = (1 * Constants.PRECISION, (100000000000000000, 2)), 
            result = 1200000000000000000
        )

    ###############################################################
    # Liquidate
    ###############################################################

    @sp.add_test(name="liquidate - successfully compounds interest and accrues fees")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy
        
        # AND a Token contract.
        governorAddress = Addresses.GOVERNOR_ADDRESS
        token = Token.FA12(
            admin = governorAddress
        )
        scenario += token

        # AND dummy contracts to act as the dev and stability funds.
        stabilityFund = DummyContract.DummyContract()
        devFund = DummyContract.DummyContract()
        scenario += stabilityFund
        scenario += devFund

        # AND a Minter contract
        liquidationFeePercent = sp.nat(80000000000000000) # 8%
        stabilityDevFundSplit = sp.nat(100000000000000000) # 10%
        minter = MinterContract(
            liquidationFeePercent = liquidationFeePercent,
            ovenProxyContractAddress = ovenProxy.address,
            stabilityFundContractAddress = stabilityFund.address,
            developerFundContractAddress = devFund.address,
            tokenContractAddress = token.address,
            stabilityFee = 100000000000000000,
            lastInterestIndexUpdateTime = sp.timestamp(0),
            interestIndex = Constants.PRECISION,
        )
        scenario += minter

        # AND the Minter is the Token administrator
        scenario += token.setAdministrator(minter.address).run(
            sender = governorAddress
        )    

        # AND a dummy contract that acts as the liquidator.
        liquidator = DummyContract.DummyContract()
        scenario += liquidator

        # AND the liquidator has $1000 of tokens.
        ovenOwnerTokens = 1000 * Constants.PRECISION 
        mintForOvenOwnerParam = sp.record(address = liquidator.address, value = ovenOwnerTokens)
        scenario += token.mint(mintForOvenOwnerParam).run(
            sender = minter.address
        )

        # WHEN liquidate is called on an undercollateralized oven with $100 of tokens outstanding
        ovenBalance = Constants.PRECISION # 1 XTZ
        ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ

        xtzPrice = Constants.PRECISION # 1 XTZ / $1

        ovenBorrowedTokens = 90 * Constants.PRECISION # $90 kUSD
        stabilityFeeTokens = sp.to_int(10 * Constants.PRECISION) # 10 kUSD

        ovenOwnerAddress = Addresses.OVEN_OWNER_ADDRESS
        ovenAddress = Addresses.OVEN_ADDRESS
        isLiquidated = False

        interestIndex = sp.to_int(Constants.PRECISION)

        liquidatorAddress = liquidator.address

        param = (xtzPrice, (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, liquidatorAddress))))))))

        # AND one period has elapsed
        now = sp.timestamp(Constants.SECONDS_PER_COMPOUND)
        scenario += minter.liquidate(param).run(
            sender = ovenProxy.address,
            amount = ovenBalanceMutez,
            now = now,
        )

        # THEN an updated interest index is sent to the oven
        scenario.verify(ovenProxy.data.updateState_interestIndex == sp.to_int(minter.data.interestIndex))

        # AND all stability fees on the oven are repaid.
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == sp.int(0))

        # AND the minter compounded interest.
        scenario.verify(minter.data.interestIndex == 1100000000000000000)
        scenario.verify(minter.data.lastInterestIndexUpdateTime == now)

        # AND the liquidator received the collateral in the oven.
        scenario.verify(liquidator.balance == ovenBalanceMutez)

        # AND the liquidator is debited the correct number of tokens.
        expectedNewlyAccruedStabilityFees = 10 * Constants.PRECISION
        outstandingTokens = sp.as_nat(stabilityFeeTokens) + ovenBorrowedTokens + expectedNewlyAccruedStabilityFees
        liquidationFee = (outstandingTokens * liquidationFeePercent) // Constants.PRECISION
        totalTokensPaid = outstandingTokens + liquidationFee
        scenario.verify(token.data.balances[liquidator.address].balance == sp.as_nat(ovenOwnerTokens - totalTokensPaid))

        # AND the stability and dev funds receive a split of the liquidation fee and stability tokens
        tokensReclaimedForFunds = liquidationFee + stabilityFeeTokens + expectedNewlyAccruedStabilityFees
        expectedDevFundTokens = (tokensReclaimedForFunds * stabilityDevFundSplit) // Constants.PRECISION
        expectedStabilityFundTokens = sp.as_nat(tokensReclaimedForFunds - expectedDevFundTokens)

        # AND the oven is marked as liquidated with values cleared correctly.
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == 0)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == True)

    @sp.add_test(name="liquidate - successfully liquidates undercollateralized oven")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy
        
        # AND a Token contract.
        governorAddress = Addresses.GOVERNOR_ADDRESS
        token = Token.FA12(
            admin = governorAddress
        )
        scenario += token

        # AND dummy contracts to act as the dev and stability funds.
        stabilityFund = DummyContract.DummyContract()
        devFund = DummyContract.DummyContract()
        scenario += stabilityFund
        scenario += devFund

        # AND a Minter contract
        liquidationFeePercent = sp.nat(80000000000000000) # 8%
        stabilityDevFundSplit = sp.nat(100000000000000000) # 10%
        minter = MinterContract(
            liquidationFeePercent = liquidationFeePercent,
            ovenProxyContractAddress = ovenProxy.address,
            stabilityFundContractAddress = stabilityFund.address,
            developerFundContractAddress = devFund.address,
            tokenContractAddress = token.address
        )
        scenario += minter

        # AND the Minter is the Token administrator
        scenario += token.setAdministrator(minter.address).run(
            sender = governorAddress
        )    

        # AND a dummy contract that acts as the liquidator.
        liquidator = DummyContract.DummyContract()
        scenario += liquidator

        # AND the liquidator has $1000 of tokens.
        ovenOwnerTokens = 1000 * Constants.PRECISION
        mintForOvenOwnerParam = sp.record(address = liquidator.address, value = ovenOwnerTokens)
        scenario += token.mint(mintForOvenOwnerParam).run(
            sender = minter.address
        )

        # WHEN liquidate is called on an undercollateralized oven.
        ovenBalance = Constants.PRECISION # 1 XTZ
        ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ

        xtzPrice = Constants.PRECISION # 1 XTZ / $1

        ovenBorrowedTokens = 2 * Constants.PRECISION # $2 kUSD

        ovenOwnerAddress =  Addresses.OVEN_OWNER_ADDRESS
        ovenAddress = Addresses.OVEN_ADDRESS
        isLiquidated = False

        stabilityFeeTokens = sp.to_int(Constants.PRECISION)
        interestIndex = sp.to_int(Constants.PRECISION)

        liquidatorAddress = liquidator.address

        param = (xtzPrice, (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, liquidatorAddress))))))))
        scenario += minter.liquidate(param).run(
            sender = ovenProxy.address,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN the liquidator received the collateral in the oven.
        scenario.verify(liquidator.balance == ovenBalanceMutez)

        # AND the liquidator is debited the correct number of tokens.
        outstandingTokens = sp.as_nat(stabilityFeeTokens) + ovenBorrowedTokens
        liquidationFee = (outstandingTokens * liquidationFeePercent) // Constants.PRECISION
        totalTokensPaid = outstandingTokens + liquidationFee
        scenario.verify(token.data.balances[liquidator.address].balance == sp.as_nat(ovenOwnerTokens - totalTokensPaid))

        # AND the stability and dev funds receive a split of the liquidation fee and stability tokens
        tokensReclaimedForFunds = liquidationFee + stabilityFeeTokens
        expectedDevFundTokens = (tokensReclaimedForFunds * stabilityDevFundSplit) // Constants.PRECISION
        expectedStabilityFundTokens = sp.as_nat(tokensReclaimedForFunds - expectedDevFundTokens)

        # AND the oven is marked as liquidated with values cleared correctly.
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == 0)
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == 0)
        scenario.verify(ovenProxy.data.updateState_interestIndex == interestIndex)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == True)

    # TODO(keefertaylor): Enable when SmartPy supports handling `failwith` in other contracts with `valid = False`
    # SEE: https://t.me/SmartPy_io/6538
    # @sp.add_test(name="liquidate - fails when liquidator has too few tokens")
    # def test():
    #     scenario = sp.test_scenario()

    #     # GIVEN an OvenProxy contract
    #     ovenProxy = MockOvenProxy.MockOvenProxyContract()
    #     scenario += ovenProxy
        
    #     # AND a Token contract.
        # governorAddress = Addresses.GOVERNOR_ADDRESS
    #     token = Token.FA12(
    #         admin = governorAddress
    #     )
    #     scenario += token

    #     # AND a Minter contract
    #     minter = MinterContract(
    #         ovenProxyContractAddress = ovenProxy.address,
    #         tokenContractAddress = token.address
    #     )
    #     scenario += minter

    #     # AND the Minter is the Token administrator
    #     scenario += token.setAdministrator(minter.address).run(
    #         sender = governorAddress
    #     )    

    #     # AND a dummy contract that acts as the liquidator.
    #     liquidator = DummyContract.DummyContract()
    #     scenario += liquidator

    #     # AND the liquidator has $1 of tokens.
    #     ovenOwnerTokens = 1 * Constants.PRECISION 
    #     mintForOvenOwnerParam = sp.record(address = liquidator.address, value = ovenOwnerTokens)
    #     scenario += token.mint(mintForOvenOwnerParam).run(
    #         sender = minter.address
    #     )

    #     # WHEN liquidating an undecollateralized requires more tokens than the liquidator has.
    #     ovenBalance = Constants.PRECISION # 1 XTZ
    #     ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ

    #     xtzPrice = Constants.PRECISION # 1 XTZ / $1

    #     ovenBorrowedTokens = 2 * Constants.PRECISION # $2 kUSD

    #     ovenOwnerAddress =  Addresses.OVEN_OWNER_ADDRESS
    #     ovenAddress = Addresses.OVEN_ADDRESS
    #     isLiquidated = False

    #     stabilityFeeTokens = sp.to_int(Constants.PRECISION)
    #     interestIndex = sp.to_int(Constants.PRECISION)

    #     liquidatorAddress = liquidator.address

    #     # THEN the call fails.
    #     param = (xtzPrice, (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, liquidatorAddress))))))))
    #     scenario += minter.liquidate(param).run(
    #         sender = ovenProxy.address,
    #         amount = ovenBalanceMutez,
    #         now = sp.timestamp_from_utc_now(),
    #         valid = False
    #     )

    @sp.add_test(name="liquidate - fails if oven is properly collateralized")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress,
        )
        scenario += minter

        # WHEN liquidate is called on an oven that exactly meets the collateralization ratios
        ovenBalance = 2 * Constants.PRECISION # 2 XTZ
        ovenBalanceMutez = sp.mutez(2000000) # 2 XTZ
        xtzPrice = Constants.PRECISION # 1 XTZ / $1
        ovenBorrowedTokens = 1 * Constants.PRECISION # $1 kUSD

        ovenOwnerAddress =  Addresses.OVEN_OWNER_ADDRESS
        ovenAddress = Addresses.OVEN_ADDRESS
        liquidatorAddress = Addresses.LIQUIDATOR_ADDRESS

        stabilityFeeTokens = sp.to_int(0)
        interestIndex = sp.to_int(Constants.PRECISION)

        isLiquidated = False

        param = (xtzPrice, (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, liquidatorAddress))))))))

        # THEN the call fails.
        scenario += minter.liquidate(param).run(
            sender = ovenProxyAddress,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )

    @sp.add_test(name="liquidate - fails if oven is already liquidated")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress,
        )
        scenario += minter

        # WHEN liquidate is called on an undercollateralized oven which is already liquidated
        ovenBalance = Constants.PRECISION # 1 XTZ
        ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ

        xtzPrice = Constants.PRECISION # 1 XTZ / $1

        ovenBorrowedTokens = 2 * Constants.PRECISION # $2 kUSD

        ovenOwnerAddress =  Addresses.OVEN_OWNER_ADDRESS
        ovenAddress = Addresses.OVEN_ADDRESS
        liquidatorAddress = Addresses.LIQUIDATOR_ADDRESS

        stabilityFeeTokens = sp.to_int(Constants.PRECISION)
        interestIndex = sp.to_int(Constants.PRECISION)

        isLiquidated = True

        param = (xtzPrice, (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, liquidatorAddress))))))))

        # THEN the call fails.
        scenario += minter.liquidate(param).run(
            sender = ovenProxyAddress,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )

    @sp.add_test(name="liquidate - fails if not called by ovenProxy")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress,
        )
        scenario += minter

        # WHEN liquidate is called on an undercollateralized oven by someone other than the oven proxy
        ovenBalance = Constants.PRECISION # 1 XTZ
        ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ

        xtzPrice = Constants.PRECISION # 1 XTZ / $1

        ovenBorrowedTokens = 2 * Constants.PRECISION # $2 kUSD

        ovenOwnerAddress =  Addresses.OVEN_OWNER_ADDRESS
        ovenAddress = Addresses.OVEN_ADDRESS
        liquidatorAddress = Addresses.LIQUIDATOR_ADDRESS

        stabilityFeeTokens = sp.to_int(Constants.PRECISION)
        interestIndex = sp.to_int(Constants.PRECISION)

        isLiquidated = False

        param = (xtzPrice, (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, liquidatorAddress))))))))

        # THEN the call fails.
        notOvenProxy = Addresses.NULL_ADDRESS
        scenario += minter.liquidate(param).run(
            sender = notOvenProxy,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )    

    ###############################################################
    # Repay
    ###############################################################

    @sp.add_test(name="repay - succeeds and compoounds interest and stability fees correctly")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy
        
        # AND a Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            stabilityFee = 100000000000000000,
            lastInterestIndexUpdateTime = sp.timestamp(0),
            interestIndex = Constants.PRECISION,
        )
        scenario += minter

        # WHEN repay is called with valid inputs
        ovenOwnerAddress =  Addresses.OVEN_OWNER_ADDRESS
        ovenAddress = Addresses.OVEN_ADDRESS
        ovenBalance = Constants.PRECISION # 1 XTZ
        ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ
        ovenBorrowedTokens = 100 * Constants.PRECISION # $100 kUSD
        isLiquidated = False
        stabilityFeeTokens = sp.int(0)
        interestIndex = sp.to_int(Constants.PRECISION)
        tokensToRepay = sp.nat(1)
        param = (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToRepay)))))))
        now = sp.timestamp(Constants.SECONDS_PER_COMPOUND)
        scenario += minter.repay(param).run(
            sender = ovenProxy.address,
            amount = ovenBalanceMutez,
            now = now,
        )

        # THEN an updated interest index and stability fee is sent to the oven.
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == ((10 * Constants.PRECISION) - tokensToRepay))
        scenario.verify(ovenProxy.data.updateState_interestIndex == sp.to_int(minter.data.interestIndex))

        # AND the minter compounded interest.
        scenario.verify(minter.data.interestIndex == 1100000000000000000)
        scenario.verify(minter.data.lastInterestIndexUpdateTime == now)

    @sp.add_test(name="repay - repays amount greater than stability fees")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a governor
        governorAddress = Addresses.GOVERNOR_ADDRESS

        # AND an OvenProxy
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy

        # AND an Oven owner
        ovenOwner = Addresses.OVEN_OWNER_ADDRESS

        # AND a Token contract.
        interimTokenAdministrator = Addresses.GOVERNOR_ADDRESS
        token = Token.FA12(
            admin = interimTokenAdministrator
        )
        scenario += token

        # AND a developer fund contract.
        developerFund = DevFund.DevFundContract(
            governorContractAddress = governorAddress,
        )
        scenario += developerFund

        # AND a stability fund contract
        stabilityFund = StabilityFund.StabilityFundContract(
            governorContractAddress = governorAddress,
        )
        scenario += stabilityFund

        # AND a Minter contract
        stabilityDevFundSplit = sp.nat(250000000000000000) # 25%
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            governorContractAddress = governorAddress,
            tokenContractAddress = token.address,
            stabilityFundContractAddress = stabilityFund.address,
            developerFundContractAddress = developerFund.address,
            stabilityFee = sp.nat(0),
            stabilityDevFundSplit = stabilityDevFundSplit
        )
        scenario += minter

        # AND the Minter is the Token administrator
        scenario += token.setAdministrator(minter.address).run(
            sender = interimTokenAdministrator
        )

        # AND the oven owner has 100 tokens.
        ovenOwnerTokens = sp.nat(100)
        mintForOvenOwnerParam = sp.record(address = ovenOwner, value = ovenOwnerTokens)
        scenario += token.mint(mintForOvenOwnerParam).run(
            sender = minter.address
        )

        # WHEN repay is called with an amount greater than stability fees
        ovenAddress = Addresses.OVEN_ADDRESS
        ovenBalance = Constants.PRECISION # 1 XTZ
        ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ
        ovenBorrowedTokens = sp.nat(12)
        isLiquidated = False
        stabilityFeeTokens = sp.int(4)
        interestIndex = sp.to_int(Constants.PRECISION)
        tokensToRepay = sp.nat(8)
        param = (ovenAddress, (ovenOwner, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToRepay)))))))
        scenario += minter.repay(param).run(
            sender = ovenProxy.address,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN the stability fees are set to zero.
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == sp.int(0))
        
        # AND the borrowed tokens is reduced by the remainder.
        expectedBorrowedTokens = sp.as_nat(ovenBorrowedTokens - sp.as_nat(tokensToRepay - sp.as_nat(stabilityFeeTokens)))
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == expectedBorrowedTokens)
        
        # AND the oven owner was debited the amount of tokens to repay.
        scenario.verify(token.data.balances[ovenOwner].balance == sp.as_nat(ovenOwnerTokens - tokensToRepay))

        # AND the stability fund and dev fund received the proportion of tokens from the stability fees paid.
        expectedDeveloperFundBalance = (sp.as_nat(stabilityFeeTokens) * stabilityDevFundSplit) // Constants.PRECISION    
        scenario.verify(token.data.balances[developerFund.address].balance == expectedDeveloperFundBalance)
        scenario.verify(token.data.balances[stabilityFund.address].balance == sp.as_nat(sp.as_nat(stabilityFeeTokens) - expectedDeveloperFundBalance))

        # AND the other values are passed back to the the oven proxy
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_interestIndex == interestIndex)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == isLiquidated)

        # AND the oven proxy received the balance of the oven.
        scenario.verify(ovenProxy.balance == ovenBalanceMutez)

    @sp.add_test(name="repay - repays amount less than stability fees")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a governor
        governorAddress = Addresses.GOVERNOR_ADDRESS

        # AND an OvenProxy
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy

        # AND an Oven owner
        ovenOwner =  Addresses.OVEN_OWNER_ADDRESS

        # AND a Token contract.
        interimTokenAdministrator = Addresses.GOVERNOR_ADDRESS
        token = Token.FA12(
            admin = interimTokenAdministrator
        )
        scenario += token

        # AND a developer fund contract.
        developerFund = DevFund.DevFundContract(
            governorContractAddress = governorAddress,
        )
        scenario += developerFund

        # AND a stability fund contract
        stabilityFund = StabilityFund.StabilityFundContract(
            governorContractAddress = governorAddress,
        )
        scenario += stabilityFund

        # AND a Minter contract
        stabilityDevFundSplit = sp.nat(250000000000000000) # 25%
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            governorContractAddress = governorAddress,
            tokenContractAddress = token.address,
            stabilityFundContractAddress = stabilityFund.address,
            developerFundContractAddress = developerFund.address,
            stabilityFee = sp.nat(0),
            stabilityDevFundSplit = stabilityDevFundSplit
        )
        scenario += minter

        # AND the Minter is the Token administrator
        scenario += token.setAdministrator(minter.address).run(
            sender = interimTokenAdministrator
        )

        # AND the oven owner has 100 tokens.
        ovenOwnerTokens = sp.nat(100)
        mintForOvenOwnerParam = sp.record(address = ovenOwner, value = ovenOwnerTokens)
        scenario += token.mint(mintForOvenOwnerParam).run(
            sender = minter.address
        )

        # WHEN repay is called with an amount less than stability fees
        ovenAddress = Addresses.OVEN_ADDRESS
        ovenBalance = Constants.PRECISION # 1 XTZ
        ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ
        ovenBorrowedTokens = sp.nat(12)
        isLiquidated = False
        stabilityFeeTokens = sp.int(5)
        interestIndex = sp.to_int(Constants.PRECISION)
        tokensToRepay = sp.nat(4)
        param = (ovenAddress, (ovenOwner, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToRepay)))))))
        scenario += minter.repay(param).run(
            sender = ovenProxy.address,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN the stability fees are reduced by the amount to repay and the borrowed tokens are the same.
        expectedStabilityFeeTokens = stabilityFeeTokens - sp.to_int(tokensToRepay)
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == expectedStabilityFeeTokens)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == ovenBorrowedTokens)
        
        # AND the oven owner was debited the amount of tokens to repay.
        scenario.verify(token.data.balances[ovenOwner].balance == sp.as_nat(ovenOwnerTokens - tokensToRepay))

        # AND the stability fund and dev fund received the proportion of tokens from the stability fees paid.
        expectedDeveloperFundBalance = (tokensToRepay * stabilityDevFundSplit) // Constants.PRECISION    
        scenario.verify(token.data.balances[developerFund.address].balance == expectedDeveloperFundBalance)
        scenario.verify(token.data.balances[stabilityFund.address].balance == sp.as_nat(tokensToRepay - expectedDeveloperFundBalance))

        # AND the other values are passed back to the the oven proxy
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_interestIndex == interestIndex)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == isLiquidated)

        # AND the oven proxy received the balance of the oven.
        scenario.verify(ovenProxy.balance == ovenBalanceMutez)

    # TODO(keefertaylor): Enable when SmartPy supports handling `failwith` in other contracts with `valid = False`
    # SEE: https://t.me/SmartPy_io/6538
    # @sp.add_test(name="repay - repays more tokens than owned")
    # def test():
    #     scenario = sp.test_scenario()

    #     # GIVEN a governor, oven proxy and oven owner address.
        # governorAddress = Addresses.GOVERNOR_ADDRESS
    #     ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
    #     ovenOwnerAddress =  Addresses.OVEN_OWNER_ADDRESS

    #     # AND a Token contract.
    #     token = Token.FA12(
    #         admin = governorAddress
    #     )
    #     scenario += token

    #     # AND a Minter contract
    #     minter = MinterContract(
    #         ovenProxyContractAddress = ovenProxyAddress,
    #         governorContractAddress = governorAddress,
    #         tokenContractAddress = token.address,
    #         stabilityFee = sp.nat(0),
    #     )
    #     scenario += minter

    #     # AND the Minter is the Token administrator
    #     scenario += token.setAdministrator(minter.address).run(
    #         sender = governorAddress
    #     )

    #     # AND the oven owner has 1 tokens.
    #     ovenOwnerTokens = sp.nat(1)
    #     mintForOvenOwnerParam = sp.record(address = ovenOwnerAddress, value = ovenOwnerTokens)
    #     scenario += token.mint(mintForOvenOwnerParam).run(
    #         sender = minter.address
    #     )

    #     # WHEN repay is called with for an amount greater than the amount owned THEN the call fails.
    #     ovenAddress = Addresses.OVEN_ADDRESS
    #     ovenBalance = Constants.PRECISION # 1 XTZ
    #     ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ
    #     ovenBorrowedTokens = sp.nat(12)
    #     isLiquidated = False
    #     stabilityFeeTokens = sp.int(5)
    #     interestIndex = sp.to_int(Constants.PRECISION)

    #     tokensToRepay = 2 * ovenOwnerTokens

    #     param = (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToRepay)))))))
    #     scenario += minter.repay(param).run(
    #         sender = ovenProxyAddress,
    #         amount = ovenBalanceMutez,
    #         now = sp.timestamp_from_utc_now(),
    #         valid = False
    #     )

    @sp.add_test(name="repay - repays greater amount than owed")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a governor, oven proxy and oven owner address.
        governorAddress = Addresses.GOVERNOR_ADDRESS
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        ovenOwnerAddress =  Addresses.OVEN_OWNER_ADDRESS

        # AND a Token contract.
        token = Token.FA12(
            admin = governorAddress
        )
        scenario += token

        # AND a Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress,
            governorContractAddress = governorAddress,
            tokenContractAddress = token.address,
            stabilityFee = sp.nat(0),
        )
        scenario += minter

        # AND the Minter is the Token administrator
        scenario += token.setAdministrator(minter.address).run(
            sender = governorAddress
        )

        # AND the oven owner has 100 tokens.
        ovenOwnerTokens = sp.nat(100)
        mintForOvenOwnerParam = sp.record(address = ovenOwnerAddress, value = ovenOwnerTokens)
        scenario += token.mint(mintForOvenOwnerParam).run(
            sender = minter.address
        )

        # WHEN repay is called with for an amount greater than is owed THEN the call fails.
        ovenAddress = Addresses.OVEN_ADDRESS
        ovenBalance = Constants.PRECISION # 1 XTZ
        ovenBalanceMutez = sp.mutez(1000000) # 1 XTZ
        ovenBorrowedTokens = sp.nat(12)
        isLiquidated = False
        stabilityFeeTokens = sp.int(5)
        interestIndex = sp.to_int(Constants.PRECISION)

        tokensToRepay = 2 * (sp.as_nat(stabilityFeeTokens) + ovenBorrowedTokens)
        param = (ovenAddress, (ovenOwnerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToRepay)))))))
        scenario += minter.repay(param).run(
            sender = ovenProxyAddress,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )

    @sp.add_test(name="repay - fails if oven is liquidated")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy

        # AND a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address
        )
        scenario += minter

        # WHEN repay is called from a liquidated oven THEN the call fails
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        ovenBalance = sp.nat(1)
        ovenBorrowedTokens = sp.nat(2)
        isLiquidated = True
        stabilityFeeTokens = sp.int(3)
        interestIndex = sp.to_int(Constants.PRECISION)
        tokensToRepay = sp.nat(1)
        param = (ovenAddress, (ownerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToRepay)))))))
        scenario += minter.repay(param).run(
            sender = ovenProxy.address,
            valid = False,
            now = sp.timestamp_from_utc_now(),
        )

    @sp.add_test(name="repay - fails if not called by oven proxy")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress
        )
        scenario += minter

        # WHEN repay is called by someone other than the oven proxy THEN the call fails
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        ovenBalance = sp.nat(1)
        ovenBorrowedTokens = sp.nat(2)
        isLiquidated = False
        stabilityFeeTokens = sp.int(3)
        interestIndex = sp.to_int(Constants.PRECISION)
        tokensToRepay = sp.nat(1)
        param = (ovenAddress, (ownerAddress, (ovenBalance, (ovenBorrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToRepay)))))))
        notOvenProxyAddress = Addresses.NULL_ADDRESS
        scenario += minter.repay(param).run(
            sender = notOvenProxyAddress,
            valid = False,
            now = sp.timestamp_from_utc_now(),
        )

    ###############################################################
    # Borrow
    ###############################################################

    @sp.add_test(name="borrow - succeeds and accrues stability fees")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract.
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy

        # AND a Minter contract.
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            stabilityFee = 100000000000000000,
            lastInterestIndexUpdateTime = sp.timestamp(0),
            interestIndex = Constants.PRECISION,
        )
        scenario += minter

        # WHEN borrow is called with valid inputs
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        isLiquidated = False

        xtzPrice = Constants.PRECISION # $1 / XTZ
        ovenBalance = 300 * Constants.PRECISION # 300 XTZ / $300
        ovenBalanceMutez = sp.mutez(300000000) # 300 XTZ / $300

        borrowedTokens =  100 * Constants.PRECISION # $100 kUSD

        interestIndex = sp.to_int(Constants.PRECISION)
        stabilityFeeTokens = sp.int(0)

        tokensToBorrow = Constants.PRECISION

        param = (xtzPrice, (ovenAddress, (ownerAddress, (ovenBalance, (borrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToBorrow))))))))

        now = sp.timestamp(Constants.SECONDS_PER_COMPOUND)
        scenario += minter.borrow(param).run(
            sender = ovenProxy.address,
            amount = ovenBalanceMutez,
            now = now,
        )

        # THEN an updated interest index and stability fee is sent to the oven.
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == sp.to_int(10 * Constants.PRECISION))
        scenario.verify(ovenProxy.data.updateState_interestIndex == sp.to_int(minter.data.interestIndex))

        # AND the minter compounded interest.
        scenario.verify(minter.data.interestIndex == 1100000000000000000)
        scenario.verify(minter.data.lastInterestIndexUpdateTime == now)

    @sp.add_test(name="borrow - succeeds and mints tokens")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy
        
        # AND a Token contract.
        governorAddress = Addresses.GOVERNOR_ADDRESS
        token = Token.FA12(
            admin = governorAddress
        )
        scenario += token

        # AND a Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            tokenContractAddress = token.address
        )
        scenario += minter

        # AND the Minter is the Token administrator
        scenario += token.setAdministrator(minter.address).run(
            sender = governorAddress
        )

        # WHEN borrow is called with valid inputs representing some tokens which are already borrowed.
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        isLiquidated = False

        xtzPrice = Constants.PRECISION # $1 / XTZ
        ovenBalance = 4 * Constants.PRECISION # 4 XTZ / $4
        ovenBalanceMutez = sp.mutez(2000000) # 4 XTZ / $4

        borrowedTokens = Constants.PRECISION # $1 kUSD

        interestIndex = sp.to_int(Constants.PRECISION)
        stabilityFeeTokens = sp.int(0)

        tokensToBorrow = Constants.PRECISION # $1 kUSD

        param = (xtzPrice, (ovenAddress, (ownerAddress, (ovenBalance, (borrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToBorrow))))))))
        scenario += minter.borrow(param).run(
            sender = ovenProxy.address,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN tokens are minted to the owner
        scenario.verify(token.data.balances[ownerAddress].balance == tokensToBorrow)

        # AND the rest of the params are passed back to the oven proxy
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_interestIndex == interestIndex)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == isLiquidated)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == (borrowedTokens + tokensToBorrow))
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == stabilityFeeTokens)

        # AND the remaining balance is passed back to the oven proxy
        scenario.verify(ovenProxy.balance == ovenBalanceMutez)

    @sp.add_test(name="borrow - succeeds and mints tokens when zero tokens are outstanding")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy
        
        # AND a Token contract.
        governorAddress = Addresses.GOVERNOR_ADDRESS
        token = Token.FA12(
            admin = governorAddress
        )
        scenario += token

        # AND a Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            tokenContractAddress = token.address
        )
        scenario += minter

        # AND the Minter is the Token administrator
        scenario += token.setAdministrator(minter.address).run(
            sender = governorAddress
        )

        # WHEN borrow is called with valid inputs and no tokens borrowed
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        isLiquidated = False

        xtzPrice = Constants.PRECISION # $1 / XTZ
        ovenBalance = 2 * Constants.PRECISION # 2 XTZ / $2
        ovenBalanceMutez = sp.mutez(2000000) # 2 XTZ / $2

        borrowedTokens = sp.nat(0)

        interestIndex = sp.to_int(Constants.PRECISION)
        stabilityFeeTokens = sp.int(0)

        tokensToBorrow = Constants.PRECISION

        param = (xtzPrice, (ovenAddress, (ownerAddress, (ovenBalance, (borrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToBorrow))))))))
        scenario += minter.borrow(param).run(
            sender = ovenProxy.address,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN tokens are minted to the owner
        scenario.verify(token.data.balances[ownerAddress].balance == tokensToBorrow)

        # AND the rest of the params are passed back to the oven proxy
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_interestIndex == interestIndex)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == isLiquidated)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == tokensToBorrow)
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == stabilityFeeTokens)

        # AND the remaining balance is passed back to the oven proxy
        scenario.verify(ovenProxy.balance == ovenBalanceMutez)

    @sp.add_test(name="borrow - Fails if oven is undercollateralized")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress,
        )
        scenario += minter

        # WHEN borrow is called with an amount that will undercollateralize the oven.
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        isLiquidated = False

        xtzPrice = Constants.PRECISION # $1 / XTZ
        ovenBalance = 2 * Constants.PRECISION # 2 XTZ / $2
        ovenBalanceMutez = sp.mutez(2000000) # 2 XTZ / $2

        borrowedTokens = sp.nat(0)

        interestIndex = sp.to_int(Constants.PRECISION)
        stabilityFeeTokens = sp.int(0)

        tokensToBorrow = ovenBalance

        # THEN the call fails.
        param = (xtzPrice, (ovenAddress, (ownerAddress, (ovenBalance, (borrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToBorrow))))))))
        scenario += minter.borrow(param).run(
            sender = ovenProxyAddress,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )

    @sp.add_test(name="borrow - fails if oven is liquidated")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress,
        )
        scenario += minter

        # WHEN borrow is called from a liquidated Oven.
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS

        isLiquidated = True

        xtzPrice = Constants.PRECISION # $1 / XTZ
        ovenBalance = 2 * Constants.PRECISION # 2 XTZ / $2
        ovenBalanceMutez = sp.mutez(2000000) # 2 XTZ / $2

        borrowedTokens = sp.nat(0)

        interestIndex = sp.to_int(Constants.PRECISION)
        stabilityFeeTokens = sp.int(0)

        tokensToBorrow = Constants.PRECISION

        param = (xtzPrice, (ovenAddress, (ownerAddress, (ovenBalance, (borrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToBorrow))))))))

        # THEN the call fails
        scenario += minter.borrow(param).run(
            sender = ovenProxyAddress,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )

    @sp.add_test(name="borrow - fails when not called by ovenProxy")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress,
        )
        scenario += minter

        # WHEN borrow is called by someone other than the OvenProxy
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS

        isLiquidated = True

        xtzPrice = Constants.PRECISION # $1 / XTZ
        ovenBalance = 2 * Constants.PRECISION # 2 XTZ / $2
        ovenBalanceMutez = sp.mutez(2000000) # 2 XTZ / $2

        borrowedTokens = sp.nat(0)

        interestIndex = sp.to_int(Constants.PRECISION)
        stabilityFeeTokens = sp.int(0)

        tokensToBorrow = Constants.PRECISION

        param = (xtzPrice, (ovenAddress, (ownerAddress, (ovenBalance, (borrowedTokens, (isLiquidated, (stabilityFeeTokens, (interestIndex, tokensToBorrow))))))))

        notOvenProxyAddress = Addresses.NULL_ADDRESS

        # THEN the call fails
        scenario += minter.borrow(param).run(
            sender = notOvenProxyAddress,
            amount = ovenBalanceMutez,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )

    ################################################################
    # Withdraw
    ################################################################

    @sp.add_test(name="withdraw - succeeds and compounds interest and stability fees correctly")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy
        
        # AND a Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            stabilityFee = 100000000000000000,
            lastInterestIndexUpdateTime = sp.timestamp(0),
            interestIndex = Constants.PRECISION,
        )
        scenario += minter

        # AND given inputs that represent a properly collateralized oven at a 200% collateralization ratio
        xtzPrice = 2 * Constants.PRECISION # $2 / XTZ
        borrowedTokens = 100 * Constants.PRECISION # $100 kUSD
        lockedCollateralMutez = sp.mutez(2100000000) # 210XTZ / $210
        lockedCollateral = 210 * Constants.PRECISION # 219 XTZ / $210

        # WHEN withdraw is called with an amount that does not under collateralize the oven
        amountToWithdrawMutez = sp.mutez(1)  
        ovenAddress = Addresses.OVEN_ADDRESS
        ovenOwnerAddress = Addresses.OVEN_OWNER_ADDRESS
        isLiquidated = False
        stabilityFeeTokens = sp.int(0)
        interestIndex = sp.to_int(Constants.PRECISION)
        param = (
            xtzPrice, (
                ovenAddress, (
                    ovenOwnerAddress, (
                        lockedCollateral, (
                            borrowedTokens, (
                                isLiquidated, (
                                    stabilityFeeTokens, (
                                        interestIndex, amountToWithdrawMutez
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        now = sp.timestamp(Constants.SECONDS_PER_COMPOUND)
        scenario += minter.withdraw(param).run(
            sender = ovenProxy.address,
            amount = lockedCollateralMutez,
            now = now,
        )

        # THEN an updated interest index and stability fee is sent to the oven.
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == sp.to_int(10 * Constants.PRECISION))
        scenario.verify(ovenProxy.data.updateState_interestIndex == sp.to_int(minter.data.interestIndex))

        # AND the minter compounded interest.
        scenario.verify(minter.data.interestIndex == 1100000000000000000)
        scenario.verify(minter.data.lastInterestIndexUpdateTime == now)

    @sp.add_test(name="withdraw - Able to withdraw")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy
        
        # AND a Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address
        )
        scenario += minter

        # AND a dummy contract that acts as the Oven owner
        dummyContract = DummyContract.DummyContract()
        scenario += dummyContract

        # AND given inputs that represent a properly collateralized oven at a 200% collateralization ratio
        xtzPrice = 1 * Constants.PRECISION # $1 / XTZ
        borrowedTokens = 10 * Constants.PRECISION # $10 kUSD
        lockedCollateralMutez = sp.mutez(21000000) # 21XTZ / $21
        lockedCollateral = 21 * Constants.PRECISION # 21 XTZ / $21

        # WHEN withdraw is called with an amount that does not under collateralize the oven
        amountToWithdrawMutez = sp.mutez(1000000) # 1 XTZ / $1 
        ovenAddress = Addresses.OVEN_ADDRESS
        ovenOwnerAddress = dummyContract.address
        isLiquidated = False
        param = (
            xtzPrice, (
                ovenAddress, (
                    ovenOwnerAddress, (
                        lockedCollateral, (
                            borrowedTokens, (
                                isLiquidated, (
                                    sp.int(0), (
                                        sp.to_int(Constants.PRECISION), amountToWithdrawMutez
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        scenario += minter.withdraw(param).run(
            sender = ovenProxy.address,
            amount = lockedCollateralMutez,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN the oven owner receives the withdrawal
        scenario.verify(dummyContract.balance == amountToWithdrawMutez)

        # AND the OvenProxy received the remainder of the collateral with correct values
        scenario.verify(ovenProxy.balance == (lockedCollateralMutez - amountToWithdrawMutez))
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == borrowedTokens)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == isLiquidated)

    @sp.add_test(name="withdraw - Able to withdraw with zero borrowed tokens")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy contract
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy
        
        # AND a Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address
        )
        scenario += minter

        # AND a dummy contract that acts as the Oven owner
        dummyContract = DummyContract.DummyContract()
        scenario += dummyContract

        # AND given inputs that represent a properly collateralized oven with zero tokens borrowed
        xtzPrice = 1 * Constants.PRECISION # $1 / XTZ
        borrowedTokens = sp.nat(0) # $0 kUSD
        lockedCollateralMutez = sp.mutez(21000000) # 21XTZ / $21
        lockedCollateral = 21 * Constants.PRECISION # 21 XTZ / $21

        # WHEN withdraw is called
        amountToWithdrawMutez = sp.mutez(1000000) # 1 XTZ / $1 
        ovenAddress = Addresses.OVEN_ADDRESS
        ovenOwnerAddress = dummyContract.address
        isLiquidated = False
        param = (
            xtzPrice, (
                ovenAddress, (
                    ovenOwnerAddress, (
                        lockedCollateral, (
                            borrowedTokens, (
                                isLiquidated, (
                                    sp.int(0), (
                                        sp.to_int(Constants.PRECISION), amountToWithdrawMutez
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        scenario += minter.withdraw(param).run(
            sender = ovenProxy.address,
            amount = lockedCollateralMutez,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN the oven owner receives the withdrawal
        scenario.verify(dummyContract.balance == amountToWithdrawMutez)

        # AND the OvenProxy received the remainder of the collateral with correct values
        scenario.verify(ovenProxy.balance == (lockedCollateralMutez - amountToWithdrawMutez))
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == borrowedTokens)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == isLiquidated)        

    @sp.add_test(name="withdraw - fails when withdraw will undercollateralize oven")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress
        )
        scenario += minter

        # AND given inputs that represent a properly collateralized oven at a 200% collateralization ratio
        xtzPrice = 1 * Constants.PRECISION # $1 / XTZ
        borrowedTokens = 10 * Constants.PRECISION # $10 kUSD
        lockedCollateralMutez = sp.mutez(21000000) # 21XTZ / $21
        lockedCollateral = 21 * Constants.PRECISION # 21 XTZ / $21

        # WHEN withdraw is called with an amount that under collateralizes the oven THEN the call fails
        amountToWithdrawMutez = sp.mutez(10000000) # 10 XTZ / $10
        param = (
            xtzPrice, (
                sp.address("tz1abmz7jiCV2GH2u81LRrGgAFFgvQgiDiaf"), (
                    sp.address("tz1abmz7jiCV2GH2u81LRrGgAFFgvQgiDiaf"), (
                        lockedCollateral, (
                            borrowedTokens, (
                                False, (
                                    sp.int(4), (
                                        sp.int(5), amountToWithdrawMutez
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        scenario += minter.withdraw(param).run(
            sender = ovenProxyAddress,
            valid = False,
            amount = lockedCollateralMutez,
            now = sp.timestamp_from_utc_now(),
        )

    @sp.add_test(name="withdraw - fails when withdraw is greater than amount")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress
        )
        scenario += minter

        # WHEN withdraw is called from an with a greater amount than collateral is available THEN the call fails
        amountMutez = sp.mutez(10)
        amount = 10 * Constants.PRECISION
        withdrawAmountMutez = sp.mutez(20)
        param = (
            sp.nat(1), (
                sp.address("tz1abmz7jiCV2GH2u81LRrGgAFFgvQgiDiaf"), (
                    sp.address("tz1abmz7jiCV2GH2u81LRrGgAFFgvQgiDiaf"), (
                        amount, (
                            sp.nat(3), (
                                False, (
                                    sp.int(4), (
                                        sp.int(5), withdrawAmountMutez
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        scenario += minter.withdraw(param).run(
            sender = ovenProxyAddress,
            valid = False,
            amount = amountMutez,
            now = sp.timestamp_from_utc_now(),
        )

    @sp.add_test(name="withdraw - succeeds even if when oven is liquidated")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress
        )
        scenario += minter

        # WHEN withdraw is called from an with a liquidated oven THEN the call fails.
        isLiquidated = True
        param = (
            Constants.PRECISION, (
                sp.address("tz1abmz7jiCV2GH2u81LRrGgAFFgvQgiDiaf"), (
                    sp.address("tz1abmz7jiCV2GH2u81LRrGgAFFgvQgiDiaf"), (
                        Constants.PRECISION, (
                            sp.nat(0), (
                                isLiquidated, (
                                    sp.int(0), (
                                        sp.to_int(Constants.PRECISION), sp.mutez(1)
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
        scenario += minter.withdraw(param).run(
            sender = ovenProxyAddress,
            now = sp.timestamp_from_utc_now(),
            amount = sp.mutez(1000000)
        )

    @sp.add_test(name="withdraw - fails when not called by oven proxy")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress
        )
        scenario += minter

        # WHEN withdraw is called from an address other than the OvenProxy THEN the call fails
        notOvenProxyAddress = Addresses.NULL_ADDRESS
        param = (sp.nat(1), (notOvenProxyAddress, (notOvenProxyAddress, (sp.nat(2), (sp.nat(3), (False, (sp.int(4), (sp.int(5), sp.mutez(6)))))))))
        scenario += minter.withdraw(param).run(
            sender = notOvenProxyAddress,
            valid = False,
            now = sp.timestamp_from_utc_now(),
        )

    ################################################################
    # Deposit
    ################################################################

    @sp.add_test(name="deposit - succeeds and compoounds interest and stability fees correctly")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy

        # AND an Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            stabilityFee = 100000000000000000,
            lastInterestIndexUpdateTime = sp.timestamp(0),
            interestIndex = Constants.PRECISION,
        )
        scenario += minter

        # WHEN deposit is called
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        stabilityFeeTokens = sp.int(0)
        interestIndex = sp.to_int(Constants.PRECISION)
        borrowedTokens = 100 * Constants.PRECISION # $100 kUSD
        balance = sp.mutez(1000000) # 1 XTZ
        balanceNat = Constants.PRECISION
        param = (
            ovenAddress, (
                ownerAddress, (
                    balanceNat, (
                        borrowedTokens, (
                            False, (
                                stabilityFeeTokens,
                                interestIndex
                            )
                        )
                    )
                )
            )
        )
        now = sp.timestamp(Constants.SECONDS_PER_COMPOUND)
        scenario += minter.deposit(param).run(
            amount = balance,
            sender = ovenProxy.address,
            now = now
        )

        # THEN an updated interest index and stability fee is sent to the oven.
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == sp.to_int(10 * Constants.PRECISION))
        scenario.verify(ovenProxy.data.updateState_interestIndex == sp.to_int(minter.data.interestIndex))

        # AND the minter compounded interest.
        scenario.verify(minter.data.interestIndex == 1100000000000000000)
        scenario.verify(minter.data.lastInterestIndexUpdateTime == now)

    @sp.add_test(name="deposit - succeeds")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy

        # AND an Minter contract
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address
        )
        scenario += minter

        # WHEN deposit is called
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        balance = sp.mutez(1)
        balanceNat = sp.nat(1)
        borrowedTokens = sp.nat(2)
        stabilityFeeTokens = sp.int(0)
        interestIndex = sp.int(1000000000000000000)
        param = (
            ovenAddress, (
                ownerAddress, (
                    balanceNat, (
                        borrowedTokens, (
                            False, (
                                stabilityFeeTokens,
                                interestIndex
                            )
                        )
                    )
                )
            )
        )
        scenario += minter.deposit(param).run(
            amount = balance,
            sender = ovenProxy.address,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN input parameters are propagated to the OvenProxy
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == borrowedTokens)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == False)
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == sp.int(0))
        scenario.verify(ovenProxy.data.updateState_interestIndex == interestIndex)

        # AND the mutez balance is sent to the oven proxy
        scenario.verify(ovenProxy.balance == balance)

    @sp.add_test(name="deposit - succeeds with no oven limit")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy

        # AND an Minter contract with no oven max.
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            ovenMax = sp.none
        )
        scenario += minter

        # WHEN deposit is called 
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        balance = sp.mutez(100000001) # 100.000001 XTZ
        balanceNat = sp.nat(100000001000000000000) # 100.000001 XTZ
        borrowedTokens = sp.nat(2)
        stabilityFeeTokens = sp.int(0)
        interestIndex = sp.int(1000000000000000000)
        param = (
            ovenAddress, (
                ownerAddress, (
                    balanceNat, (
                        borrowedTokens, (
                            False, (
                                stabilityFeeTokens,
                                interestIndex
                            )
                        )
                    )
                )
            )
        )   
        scenario += minter.deposit(param).run(
            amount = balance,
            sender = ovenProxy.address,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN input parameters are propagated to the OvenProxy
        scenario.verify(ovenProxy.data.updateState_ovenAddress == ovenAddress)
        scenario.verify(ovenProxy.data.updateState_borrowedTokens == borrowedTokens)
        scenario.verify(ovenProxy.data.updateState_isLiquidated == False)
        scenario.verify(ovenProxy.data.updateState_stabilityFeeTokens == sp.int(0))
        scenario.verify(ovenProxy.data.updateState_interestIndex == interestIndex)

        # AND the mutez balance is sent to the oven proxy
        scenario.verify(ovenProxy.balance == balance)

    @sp.add_test(name="deposit - fails if over oven max")
    def test():
        scenario = sp.test_scenario()

        # GIVEN an OvenProxy
        ovenProxy = MockOvenProxy.MockOvenProxyContract()
        scenario += ovenProxy

        # AND an Minter contract with an oven maximum
        ovenMax = sp.tez(100)
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxy.address,
            ovenMax = sp.some(ovenMax)
        )
        scenario += minter

        # WHEN deposit is called with an amount greater than the maximum
        ovenAddress = Addresses.OVEN_ADDRESS
        ownerAddress = Addresses.OVEN_OWNER_ADDRESS
        balance = ovenMax + sp.mutez(1) # 100.000001 XTZ
        balanceNat = sp.nat(100000001000000000000) # 100.000001 XTZ
        borrowedTokens = sp.nat(2)
        stabilityFeeTokens = sp.int(0)
        interestIndex = sp.int(1000000000000000000)
        param = (
            ovenAddress, (
                ownerAddress, (
                    balanceNat, (
                        borrowedTokens, (
                            False, (
                                stabilityFeeTokens,
                                interestIndex
                            )
                        )
                    )
                )
            )
        )

        # THEN the call fails.
        scenario += minter.deposit(param).run(
            amount = balance,
            sender = ovenProxy.address,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )

    @sp.add_test(name="deposit - fails when oven is liquidated")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress
        )
        scenario += minter

        # WHEN deposit is called from an with a liquidated oven THEN the call fails.
        isLiquidated = True
        param = (sp.address("tz1abmz7jiCV2GH2u81LRrGgAFFgvQgiDiaf"), (sp.address("tz1abmz7jiCV2GH2u81LRrGgAFFgvQgiDiaf"), (sp.nat(1), (sp.nat(2), (isLiquidated, (sp.int(3), (sp.int(4))))))))
        scenario += minter.deposit(param).run(
            sender = ovenProxyAddress,
            valid = False,
            now = sp.timestamp_from_utc_now(),
        )

    @sp.add_test(name="deposit - fails when not called by oven proxy")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        ovenProxyAddress = Addresses.OVEN_PROXY_ADDRESS
        minter = MinterContract(
            ovenProxyContractAddress = ovenProxyAddress
        )
        scenario += minter

        # WHEN deposit is called from an address other than the OvenProxy THEN the call fails
        notOvenProxyAddress = Addresses.NULL_ADDRESS
        param = (notOvenProxyAddress, (notOvenProxyAddress, (sp.nat(1), (sp.nat(2), (False, (sp.int(3), (sp.int(4))))))))
        scenario += minter.deposit(param).run(
            sender = notOvenProxyAddress,
            valid = False,
            now = sp.timestamp_from_utc_now(),
        )

    ################################################################
    # getInterestIndex
    ################################################################

    @sp.add_test(name="getInterestIndex - compounds interest and calls back")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        initialInterestIndex = Constants.PRECISION
        stabilityFee = Constants.PRECISION
        initialTime = sp.timestamp(0)
        minter = MinterContract(
            interestIndex = initialInterestIndex,
            stabilityFee = stabilityFee,
            lastInterestIndexUpdateTime = initialTime
        )
        scenario += minter

        # AND a dummy contract to receive the callback.
        dummyContract = DummyContract.DummyContract()
        scenario += dummyContract

        # WHEN getInterestIndex is called
        callback = sp.contract(sp.TNat, dummyContract.address, "natCallback").open_some()
        scenario += minter.getInterestIndex(callback).run(
            now = sp.timestamp_from_utc_now(),
        )

        # THEN interest is compounded in minter.
        scenario.verify(minter.data.interestIndex > initialInterestIndex)
        scenario.verify(minter.data.lastInterestIndexUpdateTime > initialTime)

        # AND the callback returned the correct value.
        scenario.verify(dummyContract.data.natValue == minter.data.interestIndex)

    @sp.add_test(name="getInterestIndex - fails with amount")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        minter = MinterContract()
        scenario += minter

        # AND a dummy contract to receive the callback.
        dummyContract = DummyContract.DummyContract()
        scenario += dummyContract

        # WHEN getInterestIndex is called with an amount THEN the call fails.
        callback = sp.contract(sp.TNat, dummyContract.address, "natCallback").open_some()
        scenario += minter.getInterestIndex(callback).run(
            amount = sp.mutez(1),
            valid = False,
            now = sp.timestamp_from_utc_now(),
        )

    ################################################################
    # updateContracts
    ################################################################

    @sp.add_test(name="updateContracts - updates contracts")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        governorAddress = Addresses.GOVERNOR_ADDRESS
        minter = MinterContract(
            governorContractAddress = governorAddress
        )
        scenario += minter

        # WHEN updateContracts is called by the governor
        newGovernorContractAddress = DummyContract.DummyContract().address
        newTokenContractAddress = DummyContract.DummyContract().address
        newOvenProxyContractAddress = DummyContract.DummyContract().address
        newStabilityFundContractAddress = DummyContract.DummyContract().address
        newdeveloperFundContractAddress = DummyContract.DummyContract().address
        newContracts = (newGovernorContractAddress, (newTokenContractAddress, (newOvenProxyContractAddress, (newStabilityFundContractAddress, newdeveloperFundContractAddress))))
        scenario += minter.updateContracts(newContracts).run(
            sender = governorAddress,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN the contracts are updated.
        scenario.verify(minter.data.governorContractAddress == newGovernorContractAddress)
        scenario.verify(minter.data.tokenContractAddress == newTokenContractAddress)
        scenario.verify(minter.data.ovenProxyContractAddress == newOvenProxyContractAddress)
        scenario.verify(minter.data.stabilityFundContractAddress == newStabilityFundContractAddress)
        scenario.verify(minter.data.developerFundContractAddress == newdeveloperFundContractAddress)

    @sp.add_test(name="updateContracts - fails if not called by governor")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        governorAddress = Addresses.GOVERNOR_ADDRESS
        minter = MinterContract(
            governorContractAddress = governorAddress
        )
        scenario += minter

        # WHEN updateContracts is called by someone other than the governor THEN the request fails
        newGovernorContractAddress = DummyContract.DummyContract().address
        newTokenContractAddress = DummyContract.DummyContract().address
        newOvenProxyContractAddress = DummyContract.DummyContract().address
        newStabilityFundContractAddress = DummyContract.DummyContract().address
        newdeveloperFundContractAddress = DummyContract.DummyContract().address
        newContracts = (newGovernorContractAddress, (newTokenContractAddress, (newOvenProxyContractAddress, (newStabilityFundContractAddress, newdeveloperFundContractAddress))))
        notGovernor = Addresses.NULL_ADDRESS
        scenario += minter.updateContracts(newContracts).run(
            sender = notGovernor,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )

    ################################################################
    # updateParams
    ################################################################

    @sp.add_test(name="updateParams - compounds interest")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract with an interest index
        governorAddress = Addresses.GOVERNOR_ADDRESS
        minter = MinterContract(
            governorContractAddress = governorAddress,
            stabilityFee = 100000000000000000,
            lastInterestIndexUpdateTime = sp.timestamp(0),
            interestIndex = 1100000000000000000,
        )
        scenario += minter

        # WHEN updateParams is called by the governor
        newStabilityFee = sp.nat(1)
        newLiquidationFeePercent = sp.nat(2)
        newCollateralizationPercentage = sp.nat(3)
        newOvenMax = sp.some(sp.tez(4))
        newParams = (newStabilityFee, (newLiquidationFeePercent, (newCollateralizationPercentage, newOvenMax)))
        now = sp.timestamp(Constants.SECONDS_PER_COMPOUND)    
        scenario += minter.updateParams(newParams).run(
            sender = governorAddress,
            now = now
        )

        # THEN the the interest is compounded.
        scenario.verify(minter.data.lastInterestIndexUpdateTime == now)
        scenario.verify(minter.data.interestIndex == 1210000000000000000)

    @sp.add_test(name="updateParams - updates parameters")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        governorAddress = Addresses.GOVERNOR_ADDRESS
        minter = MinterContract(
            governorContractAddress = governorAddress
        )
        scenario += minter

        # WHEN updateParams is called by the governor
        newStabilityFee = sp.nat(1)
        newLiquidationFeePercent = sp.nat(2)
        newCollateralizationPercentage = sp.nat(3)
        newOvenMax = sp.some(sp.tez(123))
        newParams = (newStabilityFee, (newLiquidationFeePercent, (newCollateralizationPercentage, newOvenMax)))
        scenario += minter.updateParams(newParams).run(
            sender = governorAddress,
            now = sp.timestamp_from_utc_now(),
        )

        # THEN the parameters are updated.
        scenario.verify(minter.data.stabilityFee == newStabilityFee)
        scenario.verify(minter.data.liquidationFeePercent == newLiquidationFeePercent)
        scenario.verify(minter.data.collateralizationPercentage == newCollateralizationPercentage)
        scenario.verify(minter.data.ovenMax.open_some() == newOvenMax.open_some())

    @sp.add_test(name="updateParams - fails if not called by governor")
    def test():
        scenario = sp.test_scenario()

        # GIVEN a Minter contract
        governorAddress = Addresses.GOVERNOR_ADDRESS
        minter = MinterContract(
            governorContractAddress = governorAddress
        )
        scenario += minter

        # WHEN updateParams is called by someone other than the governor THEN the request fails
        newStabilityFee = sp.nat(1)
        newLiquidationFeePercent = sp.nat(2)
        newCollateralizationPercentage = sp.nat(3)
        newOvenMax = sp.some(sp.tez(123))
        newParams = (newStabilityFee, (newLiquidationFeePercent, (newCollateralizationPercentage, newOvenMax)))
        notGovernor = Addresses.NULL_ADDRESS
        scenario += minter.updateParams(newParams).run(
            sender = notGovernor,
            now = sp.timestamp_from_utc_now(),
            valid = False
        )
